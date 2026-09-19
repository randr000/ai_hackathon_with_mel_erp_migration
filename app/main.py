"""HTTP API for the ERP migration assistant.

State is held in memory: this is a single-user prototype, so a database would be
ceremony. Restarting the server clears everything, which also makes each demo run
start from a clean slate.
"""

from __future__ import annotations

import base64
import csv
import io
import os
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import commands, voice
from .matching import explain, match_accounts
from .migration import migrate_transactions, validate_migration
from .models import Account, AccountMapping, MigratedLine, TransactionLine
from .parsing import ParseError, parse_accounts, parse_transactions

# Load .env before anything reads os.environ.
load_dotenv()

BASE_DIR = Path(__file__).resolve().parent.parent
STATIC_DIR = BASE_DIR / "static"
SAMPLE_DIR = BASE_DIR / "sample_data"

app = FastAPI(
    title="ERP Migration Assistant",
    description=(
        "Maps a source chart of accounts onto a target chart, migrates "
        "transactions, and validates the result."
    ),
    version="0.1.0",
)


class AppState:
    """Everything one migration session needs."""

    def __init__(self) -> None:
        self.source_accounts: list[Account] = []
        self.target_accounts: list[Account] = []
        self.transactions: list[TransactionLine] = []
        self.mappings: list[AccountMapping] = []
        self.migrated: list[MigratedLine] = []
        self.unmapped: list[TransactionLine] = []
        self.report = None
        self.source_filename = ""
        self.target_filename = ""
        self.transactions_filename = ""
        # Human decisions, keyed by source account number -> target number.
        # Kept separately so re-running the matcher preserves them.
        self.confirmed: dict[str, str] = {}
        # Last folder the user uploaded from or downloaded to, so the voice
        # assistant can suggest it next time.
        self.last_folder: str = ""

    def reset_mappings(self) -> None:
        self.mappings = []
        self.migrated = []
        self.unmapped = []
        self.report = None

    def has_charts(self) -> bool:
        return bool(self.source_accounts and self.target_accounts)


state = AppState()


class ConfirmRequest(BaseModel):
    source_number: str
    target_number: str


class SpeakRequest(BaseModel):
    text: str | None = None
    source_number: str | None = None


class VoiceCommandRequest(BaseModel):
    audio: str  # base64-encoded audio from the browser
    filename: str = "clip.webm"


class FolderRequest(BaseModel):
    folder: str


class ApproveTopRequest(BaseModel):
    source_number: str | None = None


def _run_matching() -> list[AccountMapping]:
    state.mappings = match_accounts(
        state.source_accounts, state.target_accounts, state.confirmed
    )
    # Any chart change invalidates a previous migration.
    state.migrated = []
    state.unmapped = []
    state.report = None
    return state.mappings


def _summary() -> dict[str, object]:
    auto = sum(1 for m in state.mappings if m.status.value == "auto")
    review = sum(1 for m in state.mappings if m.status.value == "needs_review")
    confirmed = sum(1 for m in state.mappings if m.status.value == "confirmed")
    unmapped = sum(1 for m in state.mappings if not m.target_number)
    return {
        "source_accounts": len(state.source_accounts),
        "target_accounts": len(state.target_accounts),
        "transactions": len(state.transactions),
        "mappings_total": len(state.mappings),
        "auto": auto,
        "needs_review": review,
        "confirmed": confirmed,
        "unmapped": unmapped,
        "migrated_lines": len(state.migrated),
        "files": {
            "source": state.source_filename,
            "target": state.target_filename,
            "transactions": state.transactions_filename,
        },
    }


@app.get("/api/health")
def health() -> dict[str, object]:
    return {"status": "ok", "voice": voice.usage_note()}


@app.post("/api/load")
async def load(
    source: UploadFile = File(...),
    target: UploadFile = File(...),
    transactions: UploadFile | None = File(None),
) -> dict[str, object]:
    """Load the two charts (and optionally a journal) and map them."""
    try:
        state.source_accounts = parse_accounts(
            await source.read(), source.filename or "source.csv"
        )
        state.target_accounts = parse_accounts(
            await target.read(), target.filename or "target.csv"
        )
        if transactions is not None and transactions.filename:
            state.transactions = parse_transactions(
                await transactions.read(), transactions.filename
            )
        else:
            state.transactions = []
    except ParseError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    state.source_filename = source.filename or ""
    state.target_filename = target.filename or ""
    state.transactions_filename = (transactions.filename or "") if transactions else ""
    state.confirmed = {}
    _run_matching()
    return _summary()


@app.post("/api/load-samples")
def load_samples() -> dict[str, object]:
    """Load the bundled demo data so the app is usable with zero uploads."""
    source_path = SAMPLE_DIR / "source_coa.csv"
    target_path = SAMPLE_DIR / "target_coa.csv"
    txn_path = SAMPLE_DIR / "transactions.csv"

    missing = [p.name for p in (source_path, target_path) if not p.exists()]
    if missing:
        raise HTTPException(
            status_code=404,
            detail=f"Sample data missing: {', '.join(missing)}",
        )

    state.source_accounts = parse_accounts(source_path.read_bytes(), source_path.name)
    state.target_accounts = parse_accounts(target_path.read_bytes(), target_path.name)
    state.transactions = (
        parse_transactions(txn_path.read_bytes(), txn_path.name)
        if txn_path.exists()
        else []
    )
    state.source_filename = source_path.name
    state.target_filename = target_path.name
    state.transactions_filename = txn_path.name if txn_path.exists() else ""
    state.confirmed = {}
    _run_matching()
    return _summary()


@app.get("/api/mappings")
def get_mappings(status: str | None = None) -> dict[str, object]:
    mappings = state.mappings
    if status:
        mappings = [m for m in mappings if m.status.value == status]
    return {"mappings": [m.model_dump() for m in mappings], "summary": _summary()}


@app.get("/api/source-accounts")
def source_accounts() -> dict[str, object]:
    return {"accounts": [a.model_dump() for a in state.source_accounts]}


@app.get("/api/target-accounts")
def target_accounts() -> dict[str, object]:
    return {"accounts": [a.model_dump() for a in state.target_accounts]}


@app.get("/api/transactions")
def transactions() -> dict[str, object]:
    return {"transactions": [t.model_dump() for t in state.transactions]}


@app.post("/api/confirm")
def confirm(request: ConfirmRequest) -> dict[str, object]:
    """Record a human decision for one source account and re-match."""
    if not state.has_charts():
        raise HTTPException(status_code=400, detail="Load the charts first.")
    valid = {a.account_number for a in state.target_accounts}
    if request.target_number not in valid:
        raise HTTPException(
            status_code=400,
            detail=f"Target account {request.target_number} is not in the target chart.",
        )
    if request.source_number not in {a.account_number for a in state.source_accounts}:
        raise HTTPException(
            status_code=400,
            detail=f"Source account {request.source_number} is not in the source chart.",
        )

    state.confirmed[request.source_number] = request.target_number
    _run_matching()
    mapping = next(
        (m for m in state.mappings if m.source_number == request.source_number), None
    )
    return {"mapping": mapping.model_dump() if mapping else None, "summary": _summary()}


@app.post("/api/confirm/clear")
def clear_confirmations() -> dict[str, object]:
    """Discard all human decisions and return to automatic matching."""
    state.confirmed = {}
    _run_matching()
    return _summary()


@app.post("/api/approve-top")
def approve_top(request: ApproveTopRequest) -> dict[str, object]:
    """Accept the matcher's current suggestion for one account, or for the next
    account awaiting review. Mirrors clicking the top candidate in the dropdown
    without the round trip of re-selecting it."""
    if not state.has_charts():
        raise HTTPException(status_code=400, detail="Load the charts first.")

    if request.source_number:
        mapping = next(
            (m for m in state.mappings if m.source_number == request.source_number), None
        )
        if mapping is None:
            raise HTTPException(status_code=404, detail="No such mapping.")
        target_number = mapping.target_number
        if not target_number:
            raise HTTPException(
                status_code=400,
                detail="This account has no suggested match to approve.",
            )
    else:
        # First mapping still awaiting review, in source-account order.
        pending = [m for m in state.mappings if m.status.value == "needs_review"]
        if not pending:
            raise HTTPException(status_code=400, detail="No mappings awaiting review.")
        mapping = pending[0]
        target_number = mapping.target_number
        if not target_number:
            raise HTTPException(
                status_code=400,
                detail="This account has no suggested match to approve.",
            )

    state.confirmed[mapping.source_number] = target_number
    _run_matching()
    updated = next(
        (m for m in state.mappings if m.source_number == mapping.source_number), None
    )
    return {"mapping": updated.model_dump() if updated else None, "summary": _summary()}


@app.get("/api/explain/{source_number}")
def explain_mapping(source_number: str) -> dict[str, object]:
    """Return the full reasoning for one mapping, for display or speech."""
    mapping = next((m for m in state.mappings if m.source_number == source_number), None)
    if mapping is None:
        raise HTTPException(status_code=404, detail="No such mapping.")
    return {"mapping": mapping.model_dump(), "explanation": explain(mapping)}


@app.post("/api/migrate")
def migrate() -> dict[str, object]:
    """Migrate the journal onto the target chart and validate it."""
    if not state.has_charts():
        raise HTTPException(status_code=400, detail="Load the charts first.")
    if not state.mappings:
        raise HTTPException(status_code=400, detail="Run the mapping first.")
    if not state.transactions:
        raise HTTPException(
            status_code=400,
            detail="No transactions loaded, so there is nothing to migrate.",
        )

    state.migrated, state.unmapped = migrate_transactions(
        state.transactions, state.mappings
    )
    state.report = validate_migration(state.transactions, state.migrated, state.mappings)
    return {
        "summary": _summary(),
        "report": state.report.model_dump(),
        "unmapped_lines": len(state.unmapped),
    }


@app.get("/api/report")
def get_report() -> dict[str, object]:
    if state.report is None:
        raise HTTPException(status_code=404, detail="No migration has been run yet.")
    return state.report.model_dump()


@app.get("/api/export/migrated.csv")
def export_migrated() -> Response:
    if not state.migrated:
        raise HTTPException(status_code=404, detail="No migrated data to export.")

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(
        [
            "transaction_id",
            "date",
            "source_account",
            "source_account_name",
            "target_account",
            "target_account_name",
            "amount",
            "class",
            "location",
            "memo",
        ]
    )
    for line in state.migrated:
        writer.writerow(
            [
                line.transaction_id,
                line.date,
                line.source_account,
                line.source_account_name,
                line.target_account or "",
                line.target_account_name or "",
                f"{line.amount:.2f}",
                line.cls,
                line.location,
                line.memo,
            ]
        )
    return Response(
        content=buffer.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=migrated_transactions.csv"},
    )


@app.get("/api/export/mappings.csv")
def export_mappings() -> Response:
    if not state.mappings:
        raise HTTPException(status_code=404, detail="No mappings to export.")

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(
        [
            "source_account",
            "source_name",
            "target_account",
            "target_name",
            "confidence",
            "method",
            "status",
            "reasons",
        ]
    )
    for m in state.mappings:
        writer.writerow(
            [
                m.source_number,
                m.source_name,
                m.target_number or "",
                m.target_name or "",
                f"{m.confidence:.3f}",
                m.method.value,
                m.status.value,
                " | ".join(m.reasons),
            ]
        )
    return Response(
        content=buffer.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=account_mappings.csv"},
    )


@app.get("/api/voice/status")
def voice_status() -> dict[str, object]:
    return voice.usage_note()


@app.post("/api/voice/speak")
def voice_speak(request: SpeakRequest) -> Response:
    """Synthesize speech. Cached, truncated, and disabled without a key."""
    text = request.text or ""
    if not text and request.source_number:
        mapping = next(
            (m for m in state.mappings if m.source_number == request.source_number), None
        )
        if mapping is None:
            raise HTTPException(status_code=404, detail="No such mapping.")
        text = explain(mapping)

    try:
        audio, _from_cache = voice.synthesize(text)
    except voice.ElevenLabsError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    return Response(content=audio, media_type="audio/mpeg")


@app.post("/api/voice/command")
def voice_command(request: VoiceCommandRequest) -> dict[str, object]:
    """Transcribe a spoken command, parse it, and execute it.

    This is the conversational entry point: the user speaks, Scribe turns it
    into text, commands.parse_command resolves an intent, and the endpoint
    carries it out (approve / map / migrate / download / validate / explain).
    Returns what happened plus a TTS-friendly reply the frontend can play.
    """
    try:
        audio_bytes = base64.b64decode(request.audio)
        transcript = voice.transcribe(audio_bytes, request.filename)
    except voice.ElevenLabsError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Invalid audio payload: {exc}") from exc

    cmd = commands.parse_command(transcript, state.mappings)
    reply = cmd["message"]

    if not cmd["ok"]:
        return {"transcript": transcript, **cmd, "reply": reply}

    intent = cmd["intent"]

    if intent in ("approve_top", "approve"):
        source_number = cmd.get("source_number")
        target_number = cmd.get("target_number")
        if source_number and target_number:
            state.confirmed[source_number] = target_number
        elif source_number:
            mapping = next(
                (m for m in state.mappings if m.source_number == source_number), None
            )
            if mapping and mapping.target_number:
                state.confirmed[source_number] = mapping.target_number
            elif not mapping:
                return {
                    "transcript": transcript, **cmd, "ok": False,
                    "reply": f"I couldn't find account {source_number}.",
                }
            else:
                return {
                    "transcript": transcript, **cmd, "ok": False,
                    "reply": f"Account {source_number} has no suggestion to approve.",
                }
        else:
            pending = [m for m in state.mappings if m.status.value == "needs_review"]
            if pending and pending[0].target_number:
                state.confirmed[pending[0].source_number] = pending[0].target_number
            else:
                return {
                    "transcript": transcript, **cmd, "ok": False,
                    "reply": "There are no mappings awaiting review.",
                }
        _run_matching()
        reply = f"Approved {source_number or 'the top match'}. "
        reply += f"{_summary()['needs_review']} still need review."

    elif intent == "map":
        source_number = cmd.get("source_number")
        target_number = cmd.get("target_number")
        if not source_number or not target_number:
            return {
                "transcript": transcript, **cmd, "ok": False,
                "reply": "I need both accounts. Say 'map 1000 to 10000'.",
            }
        valid_targets = {a.account_number for a in state.target_accounts}
        if target_number not in valid_targets:
            return {
                "transcript": transcript, **cmd, "ok": False,
                "reply": f"{target_number} is not in the target chart of accounts.",
            }
        state.confirmed[source_number] = target_number
        _run_matching()
        reply = f"Mapped {source_number} to {target_number}."

    elif intent == "migrate":
        if not state.transactions:
            return {
                "transcript": transcript, **cmd, "ok": False,
                "reply": "There are no transactions to migrate. Load data first.",
            }
        state.migrated, state.unmapped = migrate_transactions(
            state.transactions, state.mappings
        )
        state.report = validate_migration(
            state.transactions, state.migrated, state.mappings
        )
        reply = f"Migration complete. Validation {'passed' if state.report.passed else 'failed'}."

    elif intent == "download_mappings":
        reply = "Your mappings file is ready to download."

    elif intent == "download_migrated":
        if not state.migrated:
            return {
                "transcript": transcript, **cmd, "ok": False,
                "reply": "Run the migration first, then ask me to download.",
            }
        reply = "Your migrated transactions are ready to download."

    elif intent == "clear":
        state.confirmed = {}
        _run_matching()
        reply = "Review decisions cleared."

    elif intent == "validate":
        if state.report is None:
            return {
                "transcript": transcript, **cmd, "ok": False,
                "reply": "Run the migration first.",
            }
        reply = f"Validation {'passed' if state.report.passed else 'failed'}."

    elif intent == "explain":
        source_number = cmd.get("source_number")
        mapping = next(
            (m for m in state.mappings if m.source_number == source_number), None
        )
        if mapping is None:
            return {
                "transcript": transcript, **cmd, "ok": False,
                "reply": f"I couldn't find account {source_number}.",
            }
        reply = explain(mapping)

    elif intent == "explain_report":
        if state.report is None:
            return {
                "transcript": transcript, **cmd, "ok": False,
                "reply": "Run the migration first.",
            }
        checks = state.report.checks
        reply = f"Validation {'passed' if state.report.passed else 'failed'}. " + " ".join(
            f"{c.name}: {'passed' if c.passed else 'failed'}. " for c in checks[:4]
        )

    return {"transcript": transcript, **cmd, "ok": True, "reply": reply}


@app.get("/api/folder")
def get_folder() -> dict[str, object]:
    return {"folder": state.last_folder}


@app.post("/api/folder")
def set_folder(request: FolderRequest) -> dict[str, object]:
    state.last_folder = request.folder.strip()
    return {"folder": state.last_folder}


# Serve the single-page frontend. Mounted last so /api routes win.
if STATIC_DIR.exists():
    app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "app.main:app",
        host="127.0.0.1",
        port=int(os.getenv("PORT", "8000")),
        reload=True,
    )
