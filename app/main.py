"""HTTP API for the ERP migration assistant.

State handling works in two modes, because this app runs both as a local
long-lived process and as serverless functions on Vercel:

  * Local (default): state lives in the module-level `state` object below.
  * Serverless: the client holds the session and echoes it back with each
    request. Serverless instances are ephemeral and not guaranteed to be the
    same one twice, so anything stored in memory would vanish between calls.

Endpoints that need session data therefore accept an optional `session` body
field. When it is present it wins; when it is absent the in-memory state is
used, which keeps local development and the test suite working unchanged.
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
    version="0.2.0",
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
        self.confirmed: dict[str, str] = {}
        self.last_folder: str = ""

    def has_charts(self) -> bool:
        return bool(self.source_accounts and self.target_accounts)


state = AppState()


class SessionPayload(BaseModel):
    """The client-held session.

    Only the inputs are carried across requests (charts, journal, human
    decisions, filenames). Mappings and the validation report are pure
    functions of those inputs, so the server recomputes them rather than
    trusting the client with derived data.
    """

    source_accounts: list[Account] = []
    target_accounts: list[Account] = []
    transactions: list[TransactionLine] = []
    confirmed: dict[str, str] = {}
    source_filename: str = ""
    target_filename: str = ""
    transactions_filename: str = ""
    last_folder: str = ""


class RequestBase(BaseModel):
    """Base for request bodies that may carry a session."""

    session: SessionPayload | None = None


class ConfirmRequest(RequestBase):
    source_number: str
    target_number: str


class SpeakRequest(RequestBase):
    text: str | None = None
    source_number: str | None = None


class VoiceCommandRequest(RequestBase):
    audio: str  # base64-encoded audio from the browser
    filename: str = "clip.webm"


class FolderRequest(RequestBase):
    folder: str


class ApproveTopRequest(RequestBase):
    source_number: str | None = None


class ExplainRequest(RequestBase):
    source_number: str


class SessionOnlyRequest(RequestBase):
    """A body carrying nothing but a session, for endpoints that need no input."""


def _empty_state() -> AppState:
    return AppState()


def _state_from(session: SessionPayload | None) -> AppState:
    """Build a working state from the client's session, or the global one.

    Anything the client sent wins over the local state, so a serverless
    instance that never saw the /api/load call still has everything it needs.
    """
    if session is None:
        return state

    working = _empty_state()
    working.source_accounts = list(session.source_accounts)
    working.target_accounts = list(session.target_accounts)
    working.transactions = list(session.transactions)
    working.confirmed = dict(session.confirmed)
    working.source_filename = session.source_filename
    working.target_filename = session.target_filename
    working.transactions_filename = session.transactions_filename
    working.last_folder = session.last_folder
    return working


def _session_of(working: AppState) -> dict[str, object]:
    """Serialise the inputs the client must hold for the next call."""
    return {
        "source_accounts": [a.model_dump() for a in working.source_accounts],
        "target_accounts": [a.model_dump() for a in working.target_accounts],
        "transactions": [t.model_dump() for t in working.transactions],
        "confirmed": dict(working.confirmed),
        "source_filename": working.source_filename,
        "target_filename": working.target_filename,
        "transactions_filename": working.transactions_filename,
        "last_folder": working.last_folder,
    }


def _run_matching(working: AppState) -> list[AccountMapping]:
    working.mappings = match_accounts(
        working.source_accounts, working.target_accounts, working.confirmed
    )
    working.migrated = []
    working.unmapped = []
    working.report = None
    return working.mappings


def _summary(working: AppState) -> dict[str, object]:
    auto = sum(1 for m in working.mappings if m.status.value == "auto")
    review = sum(1 for m in working.mappings if m.status.value == "needs_review")
    confirmed = sum(1 for m in working.mappings if m.status.value == "confirmed")
    unmapped = sum(1 for m in working.mappings if not m.target_number)
    return {
        "source_accounts": len(working.source_accounts),
        "target_accounts": len(working.target_accounts),
        "transactions": len(working.transactions),
        "mappings_total": len(working.mappings),
        "auto": auto,
        "needs_review": review,
        "confirmed": confirmed,
        "unmapped": unmapped,
        "migrated_lines": len(working.migrated),
        "files": {
            "source": working.source_filename,
            "target": working.target_filename,
            "transactions": working.transactions_filename,
        },
    }


def _migrated_csv(working: AppState) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(
        [
            "transaction_id", "date", "source_account", "source_account_name",
            "target_account", "target_account_name", "amount", "class",
            "location", "memo",
        ]
    )
    for line in working.migrated:
        writer.writerow(
            [
                line.transaction_id, line.date, line.source_account,
                line.source_account_name, line.target_account or "",
                line.target_account_name or "", f"{line.amount:.2f}",
                line.cls, line.location, line.memo,
            ]
        )
    return buffer.getvalue()


def _mappings_csv(working: AppState) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(
        [
            "source_account", "source_name", "target_account", "target_name",
            "confidence", "method", "status", "reasons",
        ]
    )
    for m in working.mappings:
        writer.writerow(
            [
                m.source_number, m.source_name, m.target_number or "",
                m.target_name or "", f"{m.confidence:.3f}", m.method.value,
                m.status.value, " | ".join(m.reasons),
            ]
        )
    return buffer.getvalue()


def _csv_response(text: str, filename: str) -> Response:
    return Response(
        content=text,
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


def _mapped(working: AppState) -> dict[str, object]:
    """The standard response payload: session, summary, and mappings."""
    return {
        "session": _session_of(working),
        "summary": _summary(working),
        "mappings": [m.model_dump() for m in working.mappings],
    }


# --------------------------------------------------------------------------
# Health
# --------------------------------------------------------------------------


@app.get("/api/health")
def health() -> dict[str, object]:
    return {"status": "ok", "voice": voice.usage_note()}


# --------------------------------------------------------------------------
# Loading data
# --------------------------------------------------------------------------


@app.post("/api/load")
async def load(
    source: UploadFile = File(...),
    target: UploadFile = File(...),
    transactions: UploadFile | None = File(None),
) -> dict[str, object]:
    """Load the two charts (and optionally a journal) and map them."""
    try:
        source_accounts = parse_accounts(
            await source.read(), source.filename or "source.csv"
        )
        target_accounts = parse_accounts(
            await target.read(), target.filename or "target.csv"
        )
        txn_lines: list[TransactionLine] = []
        if transactions is not None and transactions.filename:
            txn_lines = parse_transactions(
                await transactions.read(), transactions.filename
            )
    except ParseError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    working = _empty_state()
    working.source_accounts = source_accounts
    working.target_accounts = target_accounts
    working.transactions = txn_lines
    working.source_filename = source.filename or ""
    working.target_filename = target.filename or ""
    working.transactions_filename = (transactions.filename or "") if transactions else ""
    _run_matching(working)
    return _mapped(working)


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

    working = _empty_state()
    working.source_accounts = parse_accounts(source_path.read_bytes(), source_path.name)
    working.target_accounts = parse_accounts(target_path.read_bytes(), target_path.name)
    working.transactions = (
        parse_transactions(txn_path.read_bytes(), txn_path.name)
        if txn_path.exists()
        else []
    )
    working.source_filename = source_path.name
    working.target_filename = target_path.name
    working.transactions_filename = txn_path.name if txn_path.exists() else ""
    _run_matching(working)
    return _mapped(working)


# --------------------------------------------------------------------------
# Reading mappings and raw records
# --------------------------------------------------------------------------


@app.post("/api/mappings")
def post_mappings(request: SessionOnlyRequest, status: str | None = None) -> dict[str, object]:
    """Serverless path: rebuilds mappings from the client's session."""
    working = _state_from(request.session)
    if working.has_charts():
        _run_matching(working)
    mappings = working.mappings
    if status:
        mappings = [m for m in mappings if m.status.value == status]
    return {
        "session": _session_of(working),
        "mappings": [m.model_dump() for m in mappings],
        "summary": _summary(working),
    }


@app.post("/api/source-accounts")
def post_source_accounts(request: SessionOnlyRequest) -> dict[str, object]:
    working = _state_from(request.session)
    return {
        "session": _session_of(working),
        "accounts": [a.model_dump() for a in working.source_accounts],
    }


@app.post("/api/target-accounts")
def post_target_accounts(request: SessionOnlyRequest) -> dict[str, object]:
    working = _state_from(request.session)
    return {
        "session": _session_of(working),
        "accounts": [a.model_dump() for a in working.target_accounts],
    }


@app.post("/api/transactions")
def post_transactions(request: SessionOnlyRequest) -> dict[str, object]:
    working = _state_from(request.session)
    return {
        "session": _session_of(working),
        "transactions": [t.model_dump() for t in working.transactions],
    }


# --------------------------------------------------------------------------
# Human decisions
# --------------------------------------------------------------------------


@app.post("/api/confirm")
def confirm(request: ConfirmRequest) -> dict[str, object]:
    """Record a human decision for one source account and re-match."""
    working = _state_from(request.session)
    if not working.has_charts():
        raise HTTPException(status_code=400, detail="Load the charts first.")

    valid_targets = {a.account_number for a in working.target_accounts}
    if request.target_number not in valid_targets:
        raise HTTPException(
            status_code=400,
            detail=f"Target account {request.target_number} is not in the target chart.",
        )
    if request.source_number not in {a.account_number for a in working.source_accounts}:
        raise HTTPException(
            status_code=400,
            detail=f"Source account {request.source_number} is not in the source chart.",
        )

    working.confirmed[request.source_number] = request.target_number
    _run_matching(working)
    return _mapped(working)


@app.post("/api/confirm/clear")
def clear_confirmations(request: SessionOnlyRequest) -> dict[str, object]:
    """Discard all human decisions and return to automatic matching."""
    working = _state_from(request.session)
    working.confirmed = {}
    if working.has_charts():
        _run_matching(working)
    return _mapped(working)


@app.post("/api/approve-top")
def approve_top(request: ApproveTopRequest) -> dict[str, object]:
    """Accept the matcher's current suggestion for one account, or for the next
    account awaiting review."""
    working = _state_from(request.session)
    if not working.has_charts():
        raise HTTPException(status_code=400, detail="Load the charts first.")

    if not working.mappings:
        _run_matching(working)

    if request.source_number:
        mapping = next(
            (m for m in working.mappings if m.source_number == request.source_number), None
        )
        if mapping is None:
            raise HTTPException(status_code=404, detail="No such mapping.")
    else:
        pending = [m for m in working.mappings if m.status.value == "needs_review"]
        if not pending:
            raise HTTPException(status_code=400, detail="No mappings awaiting review.")
        mapping = pending[0]

    if not mapping.target_number:
        raise HTTPException(
            status_code=400,
            detail="This account has no suggested match to approve.",
        )

    working.confirmed[mapping.source_number] = mapping.target_number
    _run_matching(working)
    return _mapped(working)


@app.post("/api/explain")
def explain_mapping(request: ExplainRequest) -> dict[str, object]:
    """Return the full reasoning for one mapping, for display or speech."""
    working = _state_from(request.session)
    if working.has_charts() and not working.mappings:
        _run_matching(working)
    mapping = next(
        (m for m in working.mappings if m.source_number == request.source_number), None
    )
    if mapping is None:
        raise HTTPException(status_code=404, detail="No such mapping.")
    return {
        "session": _session_of(working),
        "mapping": mapping.model_dump(),
        "explanation": explain(mapping),
    }


# --------------------------------------------------------------------------
# Migration, validation, export
# --------------------------------------------------------------------------


@app.post("/api/migrate")
def migrate(request: SessionOnlyRequest) -> dict[str, object]:
    """Migrate the journal onto the target chart and validate it."""
    working = _state_from(request.session)
    if not working.has_charts():
        raise HTTPException(status_code=400, detail="Load the charts first.")
    if not working.transactions:
        raise HTTPException(
            status_code=400,
            detail="No transactions loaded, so there is nothing to migrate.",
        )

    _run_matching(working)
    working.migrated, working.unmapped = migrate_transactions(
        working.transactions, working.mappings
    )
    working.report = validate_migration(
        working.transactions, working.migrated, working.mappings
    )
    return {
        "session": _session_of(working),
        "summary": _summary(working),
        "report": working.report.model_dump(),
        "unmapped_lines": len(working.unmapped),
    }


@app.post("/api/export/migrated.csv")
def export_migrated(request: SessionOnlyRequest) -> Response:
    working = _state_from(request.session)
    if not working.migrated:
        if working.has_charts() and working.transactions:
            _run_matching(working)
            working.migrated, _ = migrate_transactions(
                working.transactions, working.mappings
            )
        if not working.migrated:
            raise HTTPException(status_code=404, detail="No migrated data to export.")
    return _csv_response(_migrated_csv(working), "migrated_transactions.csv")


@app.post("/api/export/mappings.csv")
def export_mappings(request: SessionOnlyRequest) -> Response:
    working = _state_from(request.session)
    if working.has_charts() and not working.mappings:
        _run_matching(working)
    if not working.mappings:
        raise HTTPException(status_code=404, detail="No mappings to export.")
    return _csv_response(_mappings_csv(working), "account_mappings.csv")


# --------------------------------------------------------------------------
# Voice
# --------------------------------------------------------------------------


@app.post("/api/voice/status")
def voice_status() -> dict[str, object]:
    return voice.usage_note()


@app.post("/api/voice/speak")
def voice_speak(request: SpeakRequest) -> Response:
    """Synthesize speech. Cached, truncated, and disabled without a key."""
    working = _state_from(request.session)
    text = request.text or ""
    if not text and request.source_number:
        if working.has_charts() and not working.mappings:
            _run_matching(working)
        mapping = next(
            (m for m in working.mappings if m.source_number == request.source_number), None
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
    """Transcribe a spoken command, parse it, and execute it."""
    working = _state_from(request.session)

    try:
        audio_bytes = base64.b64decode(request.audio)
        transcript = voice.transcribe(audio_bytes, request.filename)
    except voice.ElevenLabsError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Invalid audio payload: {exc}") from exc

    if working.has_charts() and not working.mappings:
        _run_matching(working)
    if working.has_charts() and working.confirmed:
        _run_matching(working)

    cmd = commands.parse_command(transcript, working.mappings)
    reply = cmd["message"]
    base = {"transcript": transcript, "session": _session_of(working)}

    if not cmd["ok"]:
        return {**base, **cmd, "reply": reply}

    intent = cmd["intent"]

    if intent in ("approve_top", "approve"):
        source_number = cmd.get("source_number")
        if source_number:
            mapping = next(
                (m for m in working.mappings if m.source_number == source_number), None
            )
            if mapping is None:
                return {**base, **cmd, "ok": False,
                        "reply": f"I couldn't find account {source_number}."}
            if not mapping.target_number:
                return {**base, **cmd, "ok": False,
                        "reply": f"Account {source_number} has no suggestion to approve."}
            working.confirmed[source_number] = mapping.target_number
        else:
            pending = [
                m for m in working.mappings
                if m.status.value == "needs_review" and m.target_number
            ]
            if not pending:
                return {**base, **cmd, "ok": False,
                        "reply": "There are no mappings awaiting review."}
            working.confirmed[pending[0].source_number] = pending[0].target_number
        _run_matching(working)
        reply = (
            f"Approved {source_number or 'the top match'}. "
            f"{_summary(working)['needs_review']} still need review."
        )

    elif intent == "map":
        source_number = cmd.get("source_number")
        target_number = cmd.get("target_number")
        if not source_number or not target_number:
            return {**base, **cmd, "ok": False,
                    "reply": "I need both accounts. Say 'map 1000 to 10000'."}
        if target_number not in {a.account_number for a in working.target_accounts}:
            return {**base, **cmd, "ok": False,
                    "reply": f"{target_number} is not in the target chart of accounts."}
        working.confirmed[source_number] = target_number
        _run_matching(working)
        reply = f"Mapped {source_number} to {target_number}."

    elif intent == "migrate":
        if not working.transactions:
            return {**base, **cmd, "ok": False,
                    "reply": "There are no transactions to migrate. Load data first."}
        _run_matching(working)
        working.migrated, working.unmapped = migrate_transactions(
            working.transactions, working.mappings
        )
        working.report = validate_migration(
            working.transactions, working.migrated, working.mappings
        )
        reply = (
            f"Migration complete. Validation "
            f"{'passed' if working.report.passed else 'failed'}."
        )

    elif intent == "download_mappings":
        reply = "Your mappings file is ready to download."

    elif intent == "download_migrated":
        if not working.transactions:
            return {**base, **cmd, "ok": False,
                    "reply": "Run the migration first, then ask me to download."}
        reply = "Your migrated transactions are ready to download."

    elif intent == "clear":
        working.confirmed = {}
        _run_matching(working)
        reply = "Review decisions cleared."

    elif intent == "validate":
        if not working.transactions:
            return {**base, **cmd, "ok": False, "reply": "Run the migration first."}
        _run_matching(working)
        working.migrated, _ = migrate_transactions(working.transactions, working.mappings)
        working.report = validate_migration(
            working.transactions, working.migrated, working.mappings
        )
        reply = f"Validation {'passed' if working.report.passed else 'failed'}."

    elif intent == "explain":
        source_number = cmd.get("source_number")
        mapping = next(
            (m for m in working.mappings if m.source_number == source_number), None
        )
        if mapping is None:
            return {**base, **cmd, "ok": False,
                    "reply": f"I couldn't find account {source_number}."}
        reply = explain(mapping)

    elif intent == "explain_report":
        if not working.transactions:
            return {**base, **cmd, "ok": False, "reply": "Run the migration first."}
        _run_matching(working)
        working.migrated, _ = migrate_transactions(working.transactions, working.mappings)
        working.report = validate_migration(
            working.transactions, working.migrated, working.mappings
        )
        reply = f"Validation {'passed' if working.report.passed else 'failed'}. " + " ".join(
            f"{c.name}: {'passed' if c.passed else 'failed'}. "
            for c in working.report.checks[:4]
        )

    return {**base, **cmd, "ok": True, "reply": reply,
            "session": _session_of(working)}


@app.get("/api/folder")
def get_folder() -> dict[str, object]:
    return {"folder": state.last_folder}


@app.post("/api/folder")
def set_folder(request: FolderRequest) -> dict[str, object]:
    working = _state_from(request.session)
    working.last_folder = request.folder.strip()
    return {"folder": working.last_folder, "session": _session_of(working)}


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
