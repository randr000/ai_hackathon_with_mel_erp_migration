"""Verify the frontend/backend contract.

The session refactor renamed and re-methoded several endpoints (for example
GET /api/explain/{n} became POST /api/explain). A stale fetch path in app.js
would only surface as a 404 in the browser, so check it statically here:
every path the frontend calls must exist in the FastAPI route table, and every
call that needs the session must be a POST (GETs cannot carry a body).
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.main import app  # noqa: E402

APP_JS = (ROOT / "static" / "app.js").read_text(encoding="utf-8")

# Map registered paths to the methods that serve them.
routes: dict[str, set[str]] = {}
for route in app.routes:
    path = getattr(route, "path", None)
    methods = getattr(route, "methods", None)
    if path and methods:
        routes.setdefault(path, set()).update(methods)

failures = []


def check(label, condition, detail=""):
    print(f"[{'PASS' if condition else 'FAIL'}] {label}" + (f" -- {detail}" if detail else ""))
    if not condition:
        failures.append(label)


# Extract literal paths from fetch(...) calls, ignoring template building.
fetched = set(re.findall(r"""fetch\(\s*[\"']([^\"']+)[\"']""", APP_JS))
# Paths passed to the post() helper also hit the API.
posted = set(re.findall(r"""post\(\s*[\"']([^\"']+)[\"']""", APP_JS))
downloads = set(re.findall(r"""downloadCsv\(\s*[\"']([^\"']+)[\"']""", APP_JS))
called = fetched | posted | downloads

print("=== paths the frontend calls ===")
for path in sorted(called):
    print("   ", path)

print()
print("=== every called path is a registered route ===")
for path in sorted(called):
    if path in routes:
        check(f"{path} exists", True, ",".join(sorted(routes[path])))
    else:
        check(f"{path} exists", False, "not in the route table")

print()
print("=== session-carrying calls must be POST ===")
# These all read or mutate session data, so they must be POST: a GET has no body.
SESSION_PATHS = [
    "/api/mappings",
    "/api/load-samples",
    "/api/confirm",
    "/api/approve-top",
    "/api/confirm/clear",
    "/api/migrate",
    "/api/explain",
    "/api/source-accounts",
    "/api/target-accounts",
    "/api/transactions",
    "/api/export/migrated.csv",
    "/api/export/mappings.csv",
    "/api/voice/command",
]
for path in SESSION_PATHS:
    methods = routes.get(path, set())
    check(f"POST {path}", "POST" in methods, ",".join(sorted(methods)) or "missing")

print()
print("=== no stale GET-only endpoints remain ===")
# /api/voice/status was replaced by /api/health; /api/explain/{n} by POST /api/explain.
check("frontend does not call /api/voice/status", "/api/voice/status" not in called)
check(
    "no templated /api/explain/{n} fetch remains",
    not re.search(r"fetch\(\s*[`\"']/api/explain/", APP_JS),
)
check("frontend calls /api/health", "/api/health" in called)

print()
print("=== HTML export links do not bypass the session ===")
HTML = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
check(
    "export links are inert (handled by JS)",
    'id="export-mappings" class="link" href="#"' in HTML
    and 'id="export-migrated" class="link" href="#"' in HTML,
)

print()
if failures:
    print(f"RESULT: {len(failures)} FAILED: {failures}")
    sys.exit(1)
print("RESULT: contract check passed")
