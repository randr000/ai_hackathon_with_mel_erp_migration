"""ElevenLabs text-to-speech and speech-to-text integration.

Deliberately defensive about credits, since a hackathon balance is finite:

  * Disabled entirely when no API key is configured, and the UI says so.
  * Text is truncated to ELEVENLABS_MAX_CHARS before it leaves the machine.
  * Identical text is served from a local disk cache, so replaying an
    explanation costs nothing.
  * Only the cheapest turbo model is used by default.

Audio is written under cache/ so the browser can fetch it as a normal file.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

import httpx
from dotenv import load_dotenv

# Load .env here as well as in main.py, so this module works when used directly
# (scripts, tests, a REPL) and not only when imported through the server.
load_dotenv()

CACHE_DIR = Path("cache")

# ElevenLabs rejects very long inputs; this is a hard safety ceiling well below
# the documented limit so a stray explanation cannot blow the balance.
HARD_CHAR_CEILING = 2500


class ElevenLabsError(RuntimeError):
    """Raised when synthesis fails, with a message safe to show the user."""


def config() -> dict[str, str]:
    """Read configuration from the environment at call time.

    Reading late means editing .env and restarting the server is enough; no
    import-order surprises.
    """
    max_chars = os.getenv("ELEVENLABS_MAX_CHARS", "600")
    try:
        limit = min(int(max_chars), HARD_CHAR_CEILING)
    except ValueError:
        limit = 600
    return {
        "api_key": os.getenv("ELEVENLABS_API_KEY", "").strip(),
        "voice_id": os.getenv("ELEVENLABS_VOICE_ID", "21m00Tcm4TlvDq8ikWAM").strip(),
        "model_id": os.getenv("ELEVENLABS_MODEL_ID", "eleven_turbo_v2_5").strip(),
        "max_chars": str(limit),
        "stt_model": os.getenv("ELEVENLABS_STT_MODEL", "scribe_v1").strip(),
    }


def is_enabled() -> bool:
    return bool(config()["api_key"])


def _cache_path(text: str, voice_id: str, model_id: str) -> Path:
    digest = hashlib.sha256(f"{voice_id}|{model_id}|{text}".encode()).hexdigest()
    return CACHE_DIR / f"{digest}.mp3"


def synthesize(text: str, voice_id: str | None = None, model_id: str | None = None) -> tuple[bytes, bool]:
    """Return (mp3_bytes, from_cache).

    Raises ElevenLabsError with a user-facing message on any failure.
    """
    settings = config()
    if not settings["api_key"]:
        raise ElevenLabsError(
            "ElevenLabs is not configured. Set ELEVENLABS_API_KEY in .env and "
            "restart the server."
        )

    voice = voice_id or settings["voice_id"]
    model = model_id or settings["model_id"]

    cleaned = " ".join((text or "").split())
    if not cleaned:
        raise ElevenLabsError("There is no text to speak.")
    if len(cleaned) > int(settings["max_chars"]):
        cleaned = cleaned[: int(settings["max_chars"])].rsplit(" ", 1)[0] + "..."

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cached = _cache_path(cleaned, voice, model)
    if cached.exists():
        return cached.read_bytes(), True

    url = f"https://api.elevenlabs.io/v1/text-to-speech/{voice}"
    try:
        response = httpx.post(
            url,
            headers={
                "xi-api-key": settings["api_key"],
                "accept": "audio/mpeg",
                "content-type": "application/json",
            },
            json={
                "text": cleaned,
                "model_id": model,
                "voice_settings": {"stability": 0.5, "similarity_boost": 0.75},
            },
            timeout=60.0,
        )
    except httpx.HTTPError as exc:
        raise ElevenLabsError(f"Could not reach ElevenLabs: {exc}") from exc

    if response.status_code == 401:
        raise ElevenLabsError("ElevenLabs rejected the API key (401).")
    if response.status_code == 429:
        raise ElevenLabsError(
            "ElevenLabs rate limit or quota reached (429). Check your balance."
        )
    if response.status_code >= 400:
        raise ElevenLabsError(
            f"ElevenLabs returned HTTP {response.status_code}: "
            f"{response.text[:200]}"
        )

    audio = response.content
    if not audio:
        raise ElevenLabsError("ElevenLabs returned empty audio.")

    # Persist so a repeat request is free.
    cached.write_bytes(audio)
    return audio, False


def transcribe(audio: bytes, filename: str = "clip.webm") -> str:
    """Transcribe speech using the ElevenLabs Speech-to-Text (Scribe) API.

    Returns the transcript text. Raises ElevenLabsError with a user-facing
    message on any failure. Unlike TTS, transcription is not cached: each
    utterance is unique and re-transcribing the same clip is rare.
    """
    settings = config()
    if not settings["api_key"]:
        raise ElevenLabsError(
            "ElevenLabs is not configured. Set ELEVENLABS_API_KEY in .env."
        )

    if not audio:
        raise ElevenLabsError("No audio was recorded.")

    try:
        response = httpx.post(
            "https://api.elevenlabs.io/v1/speech-to-text",
            headers={"xi-api-key": settings["api_key"]},
            files={"file": (filename, audio, "application/octet-stream")},
            data={"model_id": settings["stt_model"]},
            timeout=120.0,
        )
    except httpx.HTTPError as exc:
        raise ElevenLabsError(f"Could not reach ElevenLabs: {exc}") from exc

    if response.status_code == 401:
        raise ElevenLabsError("ElevenLabs rejected the API key (401).")
    if response.status_code == 429:
        raise ElevenLabsError(
            "ElevenLabs rate limit or quota reached (429). Check your balance."
        )
    if response.status_code >= 400:
        raise ElevenLabsError(
            f"ElevenLabs returned HTTP {response.status_code}: "
            f"{response.text[:200]}"
        )

    payload = response.json()
    text = payload.get("text", "")
    if not text:
        raise ElevenLabsError("ElevenLabs returned an empty transcript.")
    return text


def usage_note() -> dict[str, object]:
    """Describe the current configuration for the UI's status line."""
    settings = config()
    return {
        "enabled": bool(settings["api_key"]),
        "voice_id": settings["voice_id"],
        "model_id": settings["model_id"],
        "stt_model": settings["stt_model"],
        "max_chars": int(settings["max_chars"]),
        "cached_clips": len(list(CACHE_DIR.glob("*.mp3"))) if CACHE_DIR.exists() else 0,
    }
