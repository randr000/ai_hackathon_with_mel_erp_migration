"""Verify the Vercel-deployment fixes.

Two things are checked, both of which previously caused a 500 on a serverless
platform:

  1. A read-only / unwritable cache directory must NOT break synthesis. The old
     code called CACHE_DIR.mkdir() unguarded, which raises on Vercel's read-only
     filesystem and surfaced as HTTP 500.
  2. The Vercel entrypoint (api/index.py) must import and expose `app`.
"""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

failures = []


def check(label, condition, detail=""):
    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {label}" + (f" -- {detail}" if detail else ""))
    if not condition:
        failures.append(label)


print("=== 1. Cache directory is writable-safe ===")

# Point the cache at a path that cannot be created (nonexistent drive).
os.environ["VOICE_CACHE_DIR"] = "Z:\\definitely\\not\\writable"

from app import voice  # noqa: E402  (import after env is set)

print(f"cache dir resolves to: {voice._cache_dir()}")
check(
    "unwritable cache path does not raise on mkdir",
    True,  # reaching this line means module import + resolution survived
)

# _write_cache must swallow the failure rather than propagating it.
try:
    voice._write_cache(voice._cache_dir() / "x.mp3", b"fake")
    check("_write_cache tolerates an unwritable directory", True)
except Exception as exc:  # noqa: BLE001
    check("_write_cache tolerates an unwritable directory", False, repr(exc))

# _read_cache on a missing/unreadable dir returns None, not an exception.
try:
    result = voice._read_cache(voice._cache_dir() / "missing.mp3")
    check("_read_cache returns None instead of raising", result is None, repr(result))
except Exception as exc:  # noqa: BLE001
    check("_read_cache returns None instead of raising", False, repr(exc))

# usage_note() is called by /api/health; it must not raise.
try:
    note = voice.usage_note()
    check("usage_note() works with an unwritable cache", True, str(note.get("cached_clips")))
except Exception as exc:  # noqa: BLE001
    check("usage_note() works with an unwritable cache", False, repr(exc))


print()
print("=== 2. Real synthesis with an unwritable cache ===")

if not voice.is_enabled():
    print("[SKIP] No ELEVENLABS_API_KEY configured; cannot exercise live TTS.")
else:
    try:
        audio, from_cache = voice.synthesize("Voice check.")
        check(
            "synthesize() returns audio despite an unwritable cache",
            len(audio) > 0,
            f"{len(audio)} bytes, from_cache={from_cache}",
        )
    except voice.ElevenLabsError as exc:
        check("synthesize() returns audio despite an unwritable cache", False, str(exc))


print()
print("=== 3. FastAPI entrypoint Vercel auto-detects ===")

os.environ.pop("VOICE_CACHE_DIR", None)
try:
    # Vercel's zero-config Python runtime looks for a FastAPI instance named
    # `app`; app/main.py is the module it finds. Importing it here proves the
    # module is loadable in a fresh interpreter, which is what the build needs.
    from app.main import app as fastapi_app

    check("app.main exposes an ASGI `app`", fastapi_app is not None)
    check("it is a FastAPI instance", type(fastapi_app).__name__ == "FastAPI")

    routes = {getattr(r, "path", None) for r in fastapi_app.routes}
    for expected in ("/api/health", "/api/load", "/api/migrate"):
        check(f"route {expected} is registered", expected in routes)
except Exception as exc:  # noqa: BLE001
    check("app.main imports cleanly", False, repr(exc))


print()
if failures:
    print(f"RESULT: {len(failures)} check(s) FAILED: {failures}")
    sys.exit(1)
print("RESULT: all checks passed")
