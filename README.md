# ERP Migration Assistant

An agentic assistant for ERP implementations: it maps a **source chart of
accounts** onto a **target chart of accounts**, explains every decision,
escalates the ones it is unsure about, migrates the transaction journal onto the
new accounts, and then **proves** the migration did not lose or alter a cent.

Built for the AI Hackathon. Python + FastAPI backend, dependency-free vanilla-JS
frontend, 100% open source except ElevenLabs (for spoken explanations).

## The problem it solves

Migrating off QuickBooks (or any ERP) means re-keying thousands of historical
transactions against a brand-new chart of accounts. The hard part is not moving
the data, it is deciding which old account corresponds to which new account —
and being able to defend that decision to an auditor afterwards.

This tool does the mapping, shows its work, asks a human when it is unsure, and
then validates the migrated journal arithmetically.

## Quick start

Requires Python 3.11+ (verified on 3.13).

```bash
# 1. (recommended) create a virtual environment
python -m venv .venv
.venv\Scripts\activate          # Windows
# source .venv/bin/activate     # macOS / Linux

# 2. Install dependencies
python -m pip install -r requirements.txt

# 3. (optional) configure ElevenLabs
copy .env.example .env          # then edit .env

# 4. Run
python -m uvicorn app.main:app --reload --port 8000
```

Open <http://127.0.0.1:8000> and click **Load sample data**.

> **Note on Windows:** the Microsoft Store build of Python does not ship the
> `py` launcher, so use `python` exactly as written above. If you installed
> from python.org, `py` works too.

## How it works

### 1. Mapping (the reasoning part)

Every source account is scored against every target account by combining four
independent, explainable signals:

| Signal | Weight | Rationale |
|---|---|---|
| Name similarity | 50% | Blends edit-distance with token containment and folds plurals, so `Insurance` matches `Insurance Expense` and `Miscellaneous Expense` matches `Other Operating Expenses` |
| Account type | 25% | Both sides collapsed to a coarse family (asset/liability/…) via a vendor-agnostic lookup |
| Detail type | 15% | QuickBooks-style detail types agree far more often than not |
| Number block | 10% | A weak corroborating hint, applied only for leading digits 1–6. Digits 7–9 are deliberately **not** scored: in real charts a 7xxx account is as often `Other Income` as an expense, so inferring a family there would manufacture evidence rather than report it |

The score becomes a **confidence**, and the signals that fired become the
**reasons** shown in the UI.

Confidence drives the workflow:

- **≥ 0.85 and the runner-up is well behind** → auto-mapped.
- **Ambiguous** (runner-up within 0.10) → flagged for review *even if the score
  is high*, because a near-tie is exactly the case a human should adjudicate.
  Both candidates are shown with their scores.
- **< 0.60** → no confident match; the account is surfaced with its best
  candidates so the user can pick.

The explanations are generated from the scoring signals themselves, so they are
always truthful about *why* a decision was made — they are not an LLM narrating
a guess.

### 2. Human review

Each row offers a dropdown of the ranked candidates. Choosing one records the
decision; the matcher re-runs with that decision pinned, and dependents update.
**Reset decisions** returns to pure automatic matching.

### 3. Migration

Journal lines are rewritten onto their target accounts. Amounts are copied
**verbatim** — never recalculated, never rounded.

### 4. Validation — the point of the whole exercise

Eight checks run against the migrated data:

| Check | Severity | What it catches |
|---|---|---|
| Row count preserved | error | Silently dropped lines |
| Journal total preserved | error | Any net-value loss |
| Every transaction stays balanced | error | A single leg moved to the wrong side |
| Amounts unaltered | error | Accidental recalculation |
| All transacting accounts mapped | error | Accounts with activity but no target |
| No lines without a target account | error | Partial migration |
| Target accounts exist | error | Mappings pointing at non-existent accounts |
| Low-confidence mappings reviewed | warning | Unreviewed risky decisions |

Any `error` fails the report and the UI says **do not migrate this data yet**.
Because a balanced journal nets to zero overall, and every mapping preserves the
amount and the transaction grouping, a non-zero migrated total is *proof* of a
real defect rather than a stylistic complaint.

## ElevenLabs (audio explanations)

Voice is optional and **off by default**. Without a key the app is fully
functional and the UI states that voice is unavailable.

To enable, add to `.env`:

```
ELEVENLABS_API_KEY=sk_...
```

Then use **Explain** on any mapping row, or **Read validation summary aloud**.

Three deliberate safeguards protect a finite credit balance:

1. **The cheapest model by default** — `eleven_turbo_v2_5`.
2. **Local disk cache** — audio is keyed by a hash of (text, voice, model), so
   replaying the same explanation costs **zero** credits. The header shows the
   cached-clip count.
3. **Truncation** — text is capped at `ELEVENLABS_MAX_CHARS` (default 600, hard
   ceiling 2500) *before* leaving the machine.

### Spoken commands

Hold the **🎤 Talk** button and speak. The clip goes to ElevenLabs Speech-to-Text
(`scribe_v1`), the transcript is parsed **deterministically** in
`app/commands.py` — no LLM call, so it costs nothing beyond transcription — and
the action runs.

| Say | What happens |
|---|---|
| "approve the top match" | Accepts the next mapping awaiting review |
| "approve 6100" / "approve sales commissions" | Accepts that account's suggestion |
| "map 1000 to 10000" | Pins source 1000 to target 10000 |
| "run the migration" | Runs the migration |
| "download the migrated file" | Triggers the CSV download |
| "clear my decisions" | Resets all human decisions |
| "show the validation report" | Reports validation status |
| "explain 2120" | Speaks that mapping's reasoning |

Spoken number words resolve ("one thousand" → `1000`), and account names match
by significant tokens. Ambiguous names are deliberately *not* guessed at.

## API

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/health` | Liveness + voice config status |
| `POST` | `/api/load` | Upload source + target charts (+ optional journal) |
| `POST` | `/api/load-samples` | Load the bundled demo dataset |
| `GET` | `/api/mappings?status=` | Mappings with confidence, reasons, candidates |
| `GET` | `/api/source-accounts` | Source chart, for the step-2 stat views |
| `GET` | `/api/target-accounts` | Target chart, for the step-2 stat views |
| `GET` | `/api/transactions` | Journal lines, for the step-2 stat views |
| `POST` | `/api/confirm` | Pin a source account to a chosen target |
| `POST` | `/api/approve-top` | Accept the suggestion for one account (or the next pending) |
| `POST` | `/api/confirm/clear` | Discard human decisions |
| `GET` | `/api/explain/{source}` | Full reasoning for one mapping |
| `POST` | `/api/migrate` | Rewrite the journal and validate it |
| `GET` | `/api/report` | Last validation report |
| `GET` | `/api/export/migrated.csv` | Migrated journal as CSV |
| `GET` | `/api/export/mappings.csv` | Mapping table with confidences |
| `POST` | `/api/voice/speak` | Synthesize speech (cached, truncated) |
| `POST` | `/api/voice/command` | Transcribe a spoken command and execute it |

Interactive docs at `/docs`.

## Sample data

`sample_data/` holds a realistic scenario: a 46-account QuickBooks-style source
chart, a 47-account target chart from a different ERP that **renumbers
everything** (1000→10000) and rewrites naming conventions
(`Operating Checking`→`Cash - Operating Bank Account`), plus 130 balanced
journal lines.

On the bundled data the engine auto-maps 39 of 46 accounts and flags 7 for
review — including two genuine ambiguities it refuses to guess at
(`Accrued Expenses` vs `Accrued Commissions`, which score identically at 0.77).
No account is left unmapped.

It deliberately includes hard cases: `Dues and Subscriptions` exists in the
target with no source counterpart, `Discounts & Credits` vs
`Discounts and Credits` differs only by an ampersand, and
`Advertising & Marketing`/`Software Subscriptions` compete for the
subscription-adjacent source accounts.

## Accepted file formats

Charts: CSV, XLSX, XLSM. Column headers are matched loosely and
case-insensitively, so `Account Name`, `account_name` and `Description` all
work. Transactions accept `class`/`Class` and treats `Description` as `memo`.
Parenthesised negatives `(1,234.56)` and `$` signs are parsed correctly.

## Deploying to Vercel

The app is serverless-ready. Vercel detects the FastAPI instance in
`app/main.py` automatically — no `vercel.json` and no `/api` entrypoint file are
needed, and adding them would risk overriding the working zero-config routing.
Deploy by connecting the GitHub repo, or run `vercel` in the project root.

**Required environment variables** (Vercel → Project → Settings → Environment
Variables). Set these *before* the first deploy, then redeploy — without the key
voice reports as unavailable:

| Variable | Value |
|---|---|
| `ELEVENLABS_API_KEY` | your key |
| `ELEVENLABS_VOICE_ID` | optional, defaults to Rachel |
| `ELEVENLABS_MODEL_ID` | optional, defaults to `eleven_turbo_v2_5` |
| `ELEVENLABS_STT_MODEL` | optional, defaults to `scribe_v1` |

Do **not** set `PORT`. Vercel assigns it; the app only reads it when run
directly as a script, which Vercel never does.

### How it works without server state

Serverless instances are ephemeral and a second request may land on a different,
empty instance. So the session lives in the **browser**, not the server:

- Every response from `/api/load`, `/api/load-samples`, and the mutation
  endpoints includes a `session` object — the inputs (both charts, the journal,
  filenames, and human decisions).
- The frontend stores it and sends it back with every subsequent request.
- The server replays it through the matcher, so mappings, validation and
  exports are recomputed on demand rather than remembered.

Mappings and the validation report are pure functions of those inputs, so
nothing derived is trusted from the client. `tests/test_stateless.py` proves
this by wiping the server's memory between every request — the same conditions
Vercel imposes.

The audio cache writes to the system temp directory, guarded so an unwritable
filesystem degrades to "no caching" instead of an error.

## Project layout

```
app/
  main.py       FastAPI routes, state handling (local + serverless)
  models.py     Pydantic domain models
  parsing.py    CSV/XLSX readers with loose header matching
  matching.py   Deterministic scoring engine + explanation generation
  migration.py  Journal rewrite + the eight validation checks
  commands.py   Spoken-command parser (deterministic, no LLM)
  voice.py      ElevenLabs TTS + STT client (cached, truncated, credit-safe)
static/         index.html, app.js, styles.css — no build step
sample_data/    Demo charts and journal
scripts/        Standalone verification helpers
.vercelignore   Keeps tests/scripts out of the deployment bundle
```

## Tests

69 tests covering the matcher, the validator, the spoken-command parser, and the
stateless request flow. The validator tests include negative cases — they corrupt
the migrated data (drop a line, alter an amount by one cent, flip a leg, null a
target) and assert the report **fails**. A validator that only ever passes is
worse than none.

```bash
python -m pytest tests -v
```

Two standalone checks that need live credentials or a hostile environment:

```bash
python scripts/check_voice.py    # live ElevenLabs TTS -> STT round trip
python scripts/check_deploy.py   # synthesis with an unwritable cache dir
```

## Notes and limitations

- **Deterministic matching only.** The engine is rule-based, so results are
  reproducible and free. The `reasons` field is the seam where an LLM-backed
  explainer would plug in; the scoring engine would remain the source of truth.
- **Spoken commands are parsed with rules, not an LLM.** The review vocabulary is
  small and closed, so regex parsing is predictable and free. Only the
  transcription itself costs credits.
- **Voice upload is not automated.** Browsers never expose a file's full path to
  JavaScript, so the app can remember and suggest the last-used folder but
  cannot silently pick files from it.
- **Sessions are per-browser-tab.** Because the client holds the state, two tabs
  do not share a migration session, and a page reload starts fresh.
- **Large journals make large requests.** The session carries the full journal
  on every call, which is fine at demo scale (130 lines) but would want a real
  datastore for a production dataset.
- **Currency is assumed single.** No FX conversion is attempted.
- **No authentication.** Anyone with the URL can use the deployed instance.
