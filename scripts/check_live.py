"""Live end-to-end check of the stateless flow against a running server.

Simulates Vercel by treating each request as a cold instance: nothing is
remembered server-side between calls, so the session must be carried by hand.
Exits non-zero on the first failure so it is usable in CI.
"""
import json
import sys
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:8000"
failures = []


def call(path, body=None, method="POST"):
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(
        f"{BASE}{path}", data=data, method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=90) as response:
            raw = response.read()
            ctype = response.headers.get("content-type", "")
            return response.status, (json.loads(raw) if "json" in ctype else raw)
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")


def check(label, condition, detail=""):
    print(f"[{'PASS' if condition else 'FAIL'}] {label}" + (f" -- {detail}" if detail else ""))
    if not condition:
        failures.append(label)


print("=== health (no session) ===")
status, health = call("/api/health", method="GET")
check("health returns 200 with no session", status == 200, f"status={status}")
check("voice reports enabled", bool(health.get("voice", {}).get("enabled")),
      str(health.get("voice", {}).get("enabled")))

print()
print("=== load samples -> session ===")
status, loaded = call("/api/load-samples")
check("load-samples returns 200", status == 200, f"status={status}")
session = loaded.get("session") if isinstance(loaded, dict) else None
check("response carries a session", bool(session))
if session:
    check("session has both charts", len(session["source_accounts"]) == 46
          and len(session["target_accounts"]) == 47,
          f"{len(session['source_accounts'])}/{len(session['target_accounts'])}")
    check("session carries the journal", len(session["transactions"]) == 130,
          str(len(session["transactions"])))

print()
print("=== cold-instance calls (session passed by hand) ===")

status, mappings = call("/api/mappings", {"session": session})
check("mappings rebuild from session alone", status == 200, f"status={status}")
if status == 200:
    check("46 mappings recomputed", mappings["summary"]["mappings_total"] == 46,
          str(mappings["summary"]["mappings_total"]))
    check("7 need review", mappings["summary"]["needs_review"] == 7,
          str(mappings["summary"]["needs_review"]))
    session = mappings.get("session", session)

status, accounts = call("/api/source-accounts", {"session": session})
check("source-accounts from session", status == 200 and len(accounts["accounts"]) == 46,
      f"status={status}")

status, txns = call("/api/transactions", {"session": session})
check("transactions from session", status == 200 and len(txns["transactions"]) == 130,
      f"status={status}")

print()
print("=== approve a mapping, then migrate on a cold instance ===")
status, approved = call("/api/approve-top", {"session": session, "source_number": "1000"})
check("approve-top succeeds", status == 200, f"status={status}")
if status == 200:
    session = approved["session"]
    check("decision stored in session", session["confirmed"].get("1000") == "10000",
          str(session["confirmed"]))
    check("needs-review count dropped", approved["summary"]["needs_review"] == 6,
          str(approved["summary"]["needs_review"]))

status, migrated = call("/api/migrate", {"session": session})
check("migrate works with no server state", status == 200, f"status={status}")
if status == 200:
    report = migrated["report"]
    check("validation passed", report["passed"] is True,
          str([c["name"] for c in report["checks"] if not c["passed"]]))
    check("130 lines migrated", migrated["summary"]["migrated_lines"] == 130,
          str(migrated["summary"]["migrated_lines"]))
    check("totals reconcile", report["totals"]["difference"] == 0.0,
          str(report["totals"]["difference"]))
    session = migrated["session"]

print()
print("=== exports on a cold instance ===")
status, csv_bytes = call("/api/export/migrated.csv", {"session": session})
check("migrated export returns CSV", status == 200, f"status={status}")
if status == 200:
    text = csv_bytes.decode("utf-8")
    check("export has 131 lines", len(text.strip().splitlines()) == 131,
          str(len(text.strip().splitlines())))

status, csv_bytes = call("/api/export/mappings.csv", {"session": session})
check("mappings export returns CSV", status == 200, f"status={status}")

print()
print("=== voice ===")
status, _audio = call("/api/voice/speak", {"text": "Deployment check.", "session": session})
check("speak returns audio", status == 200, f"status={status}")

print()
if failures:
    print(f"RESULT: {len(failures)} FAILED: {failures}")
    sys.exit(1)
print("RESULT: all live checks passed")
