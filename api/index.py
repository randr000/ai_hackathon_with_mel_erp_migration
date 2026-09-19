"""Vercel serverless entrypoint.

Vercel's Python runtime loads one top-level variable named `app` from a file
under api/. The real FastAPI application lives in app/main.py; this module just
exposes it, with the project root on sys.path so the `app` package resolves
(functions run from a different working directory than the repo root).
"""

from __future__ import annotations

import sys
from pathlib import Path

# Project root is the parent of this file's directory (api/ -> repo root).
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.main import app  # noqa: E402,F401

# Vercel looks for a module-level `app` (ASGI) or `handler`.
__all__ = ["app"]
