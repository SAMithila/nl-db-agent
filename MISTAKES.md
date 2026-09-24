# MISTAKES.md

Every real bug found while building and operating this system: what broke,
why, how it was fixed, and what it taught. Ordered by when each was found.

| # | Bug | Phase |
|---|-----|-------|
| 1 | LLM hallucinated view columns | 4 |
| 2 | Endpoint missing from deployment (stale container) | 6 |
| 3 | Missing imports crashed container at startup | 6 |
| 4 | Syntax error from an inline `sed` edit | 6 |
| 5 | Multipart upload failure masquerading as CORS | 7 |
| 6 | React hydration mismatch | 7 |
| 7 | `/disconnect` route never registered | 6 |
| 8 | Northwind table names hardcoded after switching to Chinook | 7 |
| 9 | RAG route reported `success: False` | 7 |
| 10 | LLM-as-judge false positives from truncated context | 7C |
| 11 | Router corrupted by an accidental paste | 7 |
| 12 | Router query rewriting silently degraded retrieval | 8 |
| 13 | Anti-hallucination prompt over-steered into refusal | 8 |
| 14 | Deployment died silently when trial billing lapsed | 8 |
| 15 | Malformed `requirements.txt` blocked deploys | 8 |
| 16 | Formatter summarised a subset and did its own arithmetic | 8 |
| 17 | Schema context omitted bridge tables; model invented a join | 8 |
| 18 | Session fallback only covered the literal string "default" | 8 |
| 19 | Router defaulted to RAG once a connected DB stopped looking like Chinook | 8 |

The patterns across all nineteen are summarised at the end.

---

## Bug 1: LLM hallucinating view columns
**Phase 4** — SQL generation

**Symptom:** "What is the total revenue by product category?" failed after 3 retries.

**Root cause:** The LLM treated the `order_revenue` view as a base table and
tried to access `oi.unit_price` and `oi.product_id`, which don't exist in the view.

**Fix:**
1. Added `order_items` to the revenue keyword mapping in `schema_inspector`
2. Added explicit view column descriptions to the `sql_generator` system prompt

**Lesson:** Views must be described with their actual columns in the schema
context — the LLM cannot infer a view's structure from its name.

---

## Eval Run 1 — Results
**Phase 4** — historical: Northwind database, SQL tiers only, before the RAG
routes existed. Superseded by the 36-query benchmark in the README.

- Overall: 96% (24/25)
- Easy: 100%, Medium: 100%, Hard: 100%
- Clarification: 66.7% (2/3)
- Failure: C003 "Which products are doing well?"
  - Root cause: vague metric pattern not in keyword list
  - Fix: added "doing well" to `AMBIGUOUS_PATTERNS`
- Avg latency: 4,580ms (GPT-4o)
- Retry rate: 0%

---

## Bug 2: SQLite file upload endpoint missing from deployment
**Phase 6** — Cloud Run deployment

**Symptom:** `/connect/sqlite-upload` returned 404 on Cloud Run despite
working locally. The browser showed "Network Error" on file upload.

**Root cause:** `git push` does not redeploy Cloud Run. The new endpoint
existed in code, but the running container was built from an older image.

**Fix:** Redeploy explicitly after every backend change:
```bash
gcloud run deploy nl-db-agent --source . --region us-central1 --allow-unauthenticated
```

**Lesson:** Know how your host deploys. Cloud Run did not watch GitHub; this
caused four separate stale-container incidents in one session.

**Superseded:** the backend moved to Render, which redeploys on every push to
`main` (see Bug 14). The underlying lesson still applies — see Bug 15, where a
deploy failed silently and production stayed on old code.

---

## Bug 3: Missing imports caused container startup failure
**Phase 6** — Cloud Run deployment

**Symptom:** The Cloud Run revision failed to start with
`NameError: name 'File' is not defined`.

**Root cause:** `UploadFile`, `File`, `Form`, `tempfile`, `shutil` and `sqlite3`
were used in the new `/connect/sqlite-upload` endpoint but never imported. The
image built successfully — Python has no compile step that checks names — and
the module failed when it was first imported at startup.

**Fix:** Added to the top of `api/main.py`:
```python
from fastapi import FastAPI, HTTPException, Header, UploadFile, File, Form
import tempfile, shutil, sqlite3
```

**Lesson:** Before deploying, actually import the module:
```bash
python -c "import api.main"
```
This catches syntax errors, missing names at module level, and missing
packages. `ast.parse` is not enough — it only checks syntax, and would have
passed this file.

The same check later caught a different problem: `python-multipart` was in
`requirements.txt` but not installed locally, so the local environment had
drifted from production. Fix: `pip install -r requirements.txt` after any
dependency change.

---

## Bug 4: Syntax error from inline sed edit
**Phase 6** — Cloud Run deployment

**Symptom:** The container failed to start with `SyntaxError: invalid syntax`
on the `sqlite_master` query line.

**Root cause:** A `sed` command meant to update one line merged two lines into one:
```python
# Broken — two statements on one line
cursor.execute("SELECT name FROM sqlite_master...")        tables = [row[0]...]
```

**Fix:** Split them back into two correctly indented lines in VS Code.

**Lesson:** Don't edit Python with `sed`. Edit in the editor, then run
`python -c "import <module>"` to confirm it still loads.

---

## Bug 5: CORS blocked on multipart upload endpoint
**Phase 7** — UI deployment

**Symptom:** The browser console showed a CORS error on `/connect/sqlite-upload`,
while other endpoints worked.

**Root cause:** Two issues compounding:
1. The deployed container was stale (Bug 2 again), so the CORS fix wasn't live.
2. axios was sending `"Content-Type": "multipart/form-data"` manually, which
   omits the required `boundary` parameter. FastAPI rejected the malformed
   request, and the browser reported the failed response as a CORS error.

**Fix:**
1. Redeployed the backend.
2. Removed the manual Content-Type header so axios sets it, with the boundary:
```typescript
// Wrong — missing boundary
const res = await axios.post(url, formData, {
  headers: { "Content-Type": "multipart/form-data" },
});

// Correct — axios adds the boundary automatically
const res = await axios.post(url, formData);
```

**Lesson:** Never set Content-Type manually for multipart/form-data. And a
failed request can masquerade as a CORS error — check the actual response
before debugging CORS configuration.

---

## Bug 6: React hydration mismatch (Error #418)
**Phase 7** — UI deployment

**Symptom:** Console showed "Minified React error #418" (hydration mismatch).
The UI rendered, but with warnings.

**Root cause:** `localStorage` was read inside `useState` initializers, which
run during Next.js server-side rendering where `window` is undefined. The
server rendered fallback values and the client rendered stored values.

**Fix:** Moved all `localStorage` reads into a `useEffect`, gated by `hydrated`:
```typescript
const [hydrated, setHydrated] = useState(false);

useEffect(() => {
  setDark(loadFromStorage("llm_sql_dark", false));
  setConnection(loadFromStorage("llm_sql_connection", { connected: false }));
  // ...
  setHydrated(true);
}, []);

if (!hydrated) return null; // after ALL hooks
```

**Lesson:** Never read `localStorage`, `window` or `document` in `useState`
initializers in Next.js. Use `useEffect` for browser-only APIs. The early
return must come after every hook declaration, because hooks can't follow a
conditional return.

---

## Bug 7: /disconnect endpoint missing from deployed API
**Phase 6** — Cloud Run deployment

**Symptom:** The browser console showed a 404 on `/disconnect/{session_id}`.
The frontend calls disconnect on page load to clear stale sessions, so the
error was visible on every visit.

**Root cause:** `disconnect` was imported from `db_connector`, but no FastAPI
route was ever defined for it.

**Fix:**
```python
@app.post("/disconnect/{session_id}")
def disconnect_database(session_id: str = "default"):
    disconnect(session_id)
    return {"success": True, "message": "Disconnected"}
```

**Lesson:** Importing a function doesn't expose it as an endpoint. After each
deploy, check `/openapi.json` to confirm every route is registered.

---

## Architecture Decision: File upload size limit
**Phase 6** — UI deployment

**Problem:** A direct multipart POST to Cloud Run hit its 32MB request body
limit. A 135MB SQLite file failed silently — the frontend showed
"Connecting..." indefinitely.

**Decision:** Accept a small-file limit for the demo. Files under ~30MB work;
the Chinook demo database is well within it.

**Production path:** Upload directly from the browser to object storage using
a signed URL, then pass the storage path to the backend. The file never passes
through the API server, so its request size limit no longer applies.

**Status:** The backend now runs on Render. Its upload limit has not been
re-measured.

---

## Bug 8: Northwind hardcoded in schema_inspector, validator and permissions
**Phase 7** — Agentic RAG

**Symptom:** After switching to Chinook, queries failed with
`no such table: Genre`. The SQL was correct, but validation ran against the
wrong database.

**Root cause:** Three files had Northwind hardcoded:
- `tools/validator.py`: `DB_PATH = "../db/dev.db"`
- `tools/schema_inspector.py`: `TABLE_KEYWORDS` with Northwind table names
- `guardrails/permissions.py`: `allowed_tables` with Northwind table names

**Fix:**
1. Updated `DB_PATH` to `chinook.db` in `validator.py`
2. Updated `TABLE_KEYWORDS` to Chinook tables in `schema_inspector.py`
3. Rewrote `permissions.py` with dynamic column-based detection

**Lesson:** Don't hardcode table names in guardrails or schema mapping. Use
dynamic inspection via SQLAlchemy `inspect()` so the system works on any
database.

**Still open:** fix 2 replaced one hardcoded list with another.
`TABLE_KEYWORDS` is still Chinook-specific — see Bug 17.

---

## Bug 9: RAG route returning success=False
**Phase 7** — Agentic RAG

**Root cause:** Two separate issues:
1. `_format_rag_response()` omitted `"success": True` from its return dict.
2. `api/main.py` used `state.execution_success` to decide success, which is
   always `False` for RAG-only routes because no SQL executes.

**Fix:**
1. Added `"success": True` to both returns in `_format_rag_response`.
2. Changed `main.py` to use `final_response.get("success")`.

**Lesson:** Each route (SQL / RAG / BOTH) has a different success signal. The
API layer must read the final response, not intermediate pipeline flags.

---

## Bug 10: LLM-as-judge false positives from insufficient context
**Phase 7C** — Evaluation

**Symptom:** The judge scored correct RAG answers as hallucinations. R001's
answer "+4.8% growth", correctly cited from IFPI GMR 2025, got faithfulness=1
and hallucination=true.

**Root cause:** `rag_context` passed to the judge was truncated to 500
characters. The 4.8% figure sat beyond the cutoff, so the judge couldn't
verify the claim and assumed it was invented.

**Fix:** Pass 3,000+ characters of context to the judge. Better still, pass
each retrieved chunk as a separate block rather than one concatenated string.

**Production implications:**
- False hallucination flags corrupt RLHF training signals
- False positives in monitoring waste engineering time
- Deployment gates built on a flawed metric block good answers

**Lesson:** LLM-as-judge needs enough context to verify a claim. Validate
judge calibration against human-labelled ground truth before using its scores
as a training signal or a deployment gate.

---

## Bug 11: Router corrupted by accidental paste
**Phase 7** — Agentic RAG

**Symptom:** Every query's trace read "Route: SQL — Error — defaulting to SQL:
name 'answer' is not defined". All three routes fell back to SQL.

**Root cause:** While adding `"success": True` to `_format_rag_response()` in
`graph.py`, the return dict was pasted into `route_question()` in `router.py`,
where `answer` doesn't exist. Every routing call raised a `NameError`, which
was caught and silently defaulted to SQL.

**Fix:** Removed the corrupted block and restored the correct return:
```python
return {
    "route":     Route(route_str),
    "reason":    result.get("reason", ""),
    "sql_focus": result.get("sql_focus"),
    "rag_focus": result.get("rag_focus"),
}
```

**Lesson:** When editing several files at once, check each one separately
before committing. `grep -n "answer" agent/router.py` would have caught it
instantly. Also: a broad `except` that defaults to a working path hides
breakage — the system kept answering, just always via SQL.

---

## Bug 12: Router query rewriting silently degraded RAG retrieval
**Phase 8** — Production revival

**Symptom:** RAG questions returned "the documents do not specify…" even when
the fact was in the corpus. Retrieval reported success and cited the correct
source documents.

**Root cause:** `rag_node` used `search_query = state.rag_focus or state.question`.
The router set `rag_focus` to a document title ("IFPI Global Music Report
2025 & 2026"), so semantic search ran against a title instead of the user's
question. It returned report boilerplate; the chunk containing the answer
ranked outside top_k.

**Why it was hard to find:** every layer downstream behaved correctly on the
wrong input. Retrieval succeeded, sources were attached, no error was raised,
and the model's refusal was the right response to what it was given. I
diagnosed it as context truncation, then as a context-passing bug, then as
prompt over-steering, before reading the trace line that showed the actual
embedding query.

**Fix:**
```python
search_query = f"{state.question} {state.rag_focus}".strip() if state.rag_focus else state.question
```
The user's words lead; the router's hint is additive, never a replacement.

**Lesson:** Any rewriting between user input and retrieval is a silent
failure surface. Log the query that is actually embedded, not just the
user's question — that trace line is what found this.

---

## Bug 13: Anti-hallucination prompt over-steered into refusal
**Phase 8** — Production revival

**Symptom:** With the correct fact present in its context, the RAG formatter
still declined to answer.

**Root cause:** The system prompt said "Answer based ONLY on the provided
document context" and "If the context doesn't contain enough information,
say so clearly." Together these push toward refusal. PDF extraction had also
split a decimal ("4. 8%"), making the figure harder to recognise.

**Fix:** Removed "ONLY", told the model to expect split numbers from PDF
extraction, and made refusal conditional on genuinely not finding the answer.

**Note:** a real improvement, but it treated a symptom. The root cause was
Bug 12.

**Lesson:** Bugs 10 and 13 are grounding failures in opposite directions. The
judge flagged correct answers as hallucinated (false positive); the formatter
refused an answer it had (false negative). Neither shows up in aggregate
accuracy — in both cases the system looks like it's working.

---

## Bug 14: Deployment died silently when trial billing lapsed
**Phase 8** — Production revival

**Symptom:** The public demo returned "API connection failed". Discovered 77
days after it broke.

**Root cause:** Google Cloud trial credits expired and the billing account
closed, so Cloud Run refused to start the container. Because the container
never ran, there were no application logs — which made it look like a code
fault rather than an account problem.

**Fix:** Migrated the backend to Render (free tier, Docker, Singapore region).
Rotated API keys, moved them to the host's secret store, and added a spend cap.

**Lesson:** A demo without a health check is a liability. The README
advertised a link that had been dead for two and a half months.

**Resolved:** `/health` now actually checks OpenAI (`models.list()`),
Pinecone (`describe_index_stats()` on the live index), and the default
Chinook database (`test_connection`), returning 503 naming which check(s)
failed instead of always returning 200. A GitHub Action
(`.github/workflows/health-check.yml`) curls it once a day and fails the
workflow — which notifies via GitHub — on any non-200 response.

---

## Bug 15: Malformed requirements.txt blocked deploys while I tested stale production code
**Phase 8** — Production revival

**Symptom:** Fixes verified locally didn't change production behaviour.

**Root cause:** `echo "python-multipart" >> requirements.txt` appended to a
file with no trailing newline, producing `pypdf==5.1.0python-multipart`. Every
deploy failed at `pip install` in about 20 seconds, and production stayed on
the last good commit.

**Why it wasted time:** the symptom — a fix appearing not to work — pointed at
the fix, not the pipeline. Two rounds of re-diagnosis happened before anyone
checked deploy status.

**Fix:** Split the line in an editor.

**Lesson:** Confirm the deploy landed before re-testing in production. Check
the commit SHA and deploy status first, not last. And edit dependency files
in an editor, not with `echo >>`.

---

## Bug 16: Formatter summarised a subset and did its own arithmetic
**Phase 8** — Production revival

**Symptom:** "What is the total revenue by genre?" answered "Alternative &
Punk generated the highest revenue at $241.56." The chart directly below
showed Rock at $826.65. Data correct, chart correct, text wrong.

**Root cause:** `formatter.py` passed only `rows[:10]` to the LLM. The
generated SQL had `GROUP BY` but no `ORDER BY`, so rows came back
alphabetically and Rock was row 18. The model saw 10 of 24 genres and
reported the maximum of that subset as the overall maximum.

**First fix:** pass up to 50 rows. The executor already caps results at 100.

**What that exposed:** the answer now named Rock correctly, but stated total
revenue as $2,054.83. The real total is $2,328.60. With all 24 rows visible,
the model still summed them wrong. It also misranked second place, saying
Alternative & Punk "follows" Rock when Latin ($382.14) and Metal ($261.36)
are both higher.

**Second fix:** `_compute_facts()` computes the sum, the top three in order,
and the lowest value in Python, and passes them to the model as exact figures
it is told not to recalculate. ID columns are excluded, since an ID is a
number but not a measure.

**Lesson:** giving the model more data didn't fix a reasoning error — it
moved it. Arithmetic and ranking over a result set belong in code. The first
fix made the answer look right while it was still wrong, which is the most
dangerous state a system can be in.

The artist question, first suspected to be the same bug, turned out to be a
separate schema problem (Bug 17).

---

## Bug 17: Schema context omitted bridge tables; model invented a join
**Phase 8** — Production revival

**Symptom:** "Who are our top 5 artists by revenue?" — one of the sidebar
example questions — failed with `no such column: t.ArtistId`. The user saw a
generic "Query execution failed"; the real SQLite error was discarded.

**Root cause:** table selection is keyword-based. The question matched
Artist, Invoice and InvoiceLine, but not Track or Album — the tables that
connect them (InvoiceLine → Track → Album → Artist). With no bridge in
context, the model guessed a direct Track→Artist join that doesn't exist in
Chinook. The genre question had the same gap — Track was missing — but worked
because the model happened to remember Chinook's structure.

**Fix:** after keyword matching, walk the foreign-key graph and add the tables
on the shortest join path between those selected. It uses FK data that
`get_schema()` already collected and never used, and works on any database.
Result: 10 of 10 runs succeeded.

**What the evaluation missed:** the Hard tier reports 100%, but this question
isn't in the evaluation set. The example questions a visitor is most likely
to click should be in the benchmark.

**Harness lesson:** an intermediate test re-executed the generated SQL itself
instead of reading the agent's own success flag, and its results disagreed
with the agent's. Measure the system's output, not a reconstruction of it.

**Known limitations:**
- FK expansion only connects tables that keyword matching already chose.
  "Top selling products" still misses Track, because "product" matches no
  keyword.
- `TABLE_KEYWORDS` remains hardcoded to Chinook (see Bug 8).
- Execution errors still surface to users as a generic message. The real
  error should be logged, and should feed the retry loop.

---

## Bug 18: Session fallback only covered the literal string "default"
**Phase 8** — Production revival

**Symptom:** every SQL question failed in production with a generic
"Something went wrong" error. RAG questions kept working. The demo had never
been connected to by any visitor before asking a question, so this hit every
real user, not an edge case.

**Root cause:** two changes landed in the same commit and each was correct in
isolation, but together they broke the default demo path. `agent/graph.py`
was fixed to thread the real `state.session_id` through `schema_node`,
`validate_node`, and `execute_node` instead of silently defaulting every
stage to the literal string `"default"` — a real bug, since it meant an
uploaded database was previously being ignored and every query secretly ran
against Chinook regardless of session. In the same commit,
`db_connector.get_active_engine()` was rewritten so that only a session_id
equal to the literal string `"default"` fell back to the demo database;
any other unconnected session_id raised `LookupError`. Before that commit,
the fallback applied to any session with no active connection. Once the
session-threading fix made the real per-visitor session_id reach
`get_active_engine()`, every demo visitor — whose session_id is never
literally `"default"` — hit the `LookupError` branch on every SQL question.
The exception was caught by `get_schema()`'s generic `except Exception`,
converted to `"Something went wrong..."` by `formatter.format_error()`, and
the real error was discarded before reaching a response or a log. RAG was
unaffected because `rag_node` never calls `db_connector`.

**Fix:** restored the fallback in `get_active_engine()` to apply to *any*
session_id without an active entry in `_connections`, not just the literal
string `"default"`. A session with an explicit connection (via `/connect`)
still uses its own engine — the genuine improvement from the session-threading
fix stays intact. This keys the fallback on "does this session have an active
connection," matching the contract `get_connection_info()` already assumed
elsewhere, rather than on a specific string.

Separately, this closed part of Bug 17's known limitation: the real error was
being silently discarded (caught, stringified, dropped) with nothing recorded
anywhere. `api/main.py` now logs the real exception server-side
(`logger.error`) on every query failure, including the outer unhandled-
exception handler, which was previously also leaking `str(e)` straight into
the client-visible HTTP response — that leak was closed too, so the client
only ever sees the existing friendly message while the real error is always
recoverable from server logs.

**Lesson:** two individually-correct changes in the same commit can compose
into a regression that neither change's own tests would catch, because each
one only tested the case it was designed to fix (a literal `"default"`
session parity check, and an uploaded-database routing check) — neither
tested an unconnected non-"default" session, which is the actual shape of a
first-time demo visitor. Test the state a real user starts in, not just the
states a fix was written to handle. Also: an `except Exception: return
{"success": False, "error": str(e)}` at a component boundary will keep
converting real, actionable errors into generic ones for anyone who doesn't
already know to grep for it — errors caught at a boundary need a log
statement at the point they're caught, not just at first discovery.

---

## Bug 19: Router defaulted to RAG once a connected DB stopped looking like Chinook
**Phase 8** — Production revival

**Symptom:** with a user's own database connected (tested with a World Cup
dataset — `host_cities`, `matches`, `teams`, `tournament_stages`, no relation
to music), a plain factual question about a table that exists in that
database ("host cities?", "Show the geolocation data" against
`db/ecommerce.db`) routed to RAG. RAG searched the demo's fixed IFPI/Spotify/
Luminate PDFs, found nothing relevant, and returned a failure — even though
the answer was one `SELECT` away in the connected schema.

**Root cause:** `route_question()` took no `session_id` and had no way to
know a non-demo database was connected. Its LLM system prompt hardcoded
"SQL DATABASE (Chinook Music Store)" with Chinook's own table list and
described the document corpus as the other fixed, always-relevant source.
Once a connected table or question didn't resemble Chinook, the model made a
locally correct inference under a false premise — "not available in the
Chinook database" — and fell through to RAG by elimination. This wasn't a
crash or a swallowed exception like most bugs here; it was a well-reasoned
wrong answer, because nothing in the prompt told the model the SQL source
had changed out from under it.

**Fix:** `route_question()` now accepts `session_id` and checks
`db_connector.get_connection_info()` — the same connected/not-connected
signal Bug 18's fix relies on, not a new one. When a custom database is
connected, routing fast-paths to SQL unless the question names the fixed
document corpus explicitly (IFPI/Spotify/Luminate/"global music industry");
if it does, the LLM router runs against a prompt built from the *connected*
database's live schema (via the same `get_schema()` used by Bug 17's FK-path
fix) instead of Chinook. A session with no `/connect` call takes the exact
same code path as before this change — demo-mode routing is untouched.

**Lesson:** a router (or any component) tuned against one fixed domain will
silently keep reasoning as if that domain is still true once the environment
changes underneath it — "not found in X" is a valid inference right up until
X is no longer the only place to look, and nothing forces the model to
notice. Any prompt that hardcodes a specific schema/corpus needs the same
connection-awareness the rest of the pipeline already has, not just the
components that touch the database directly. Same root shape as Bug 8/17
(hardcoded assumptions break on database switch), but distinct in outcome:
those were malformed SQL or a crash; this was a confidently wrong choice with
no error to notice at all.

---

## Patterns

**Silent failures at the handoffs (Bugs 11, 12, 13, 16, 17).** The most
expensive bugs raised no errors. Each component behaved correctly on the input
it received; the fault lived in what one component handed the next — a
rewritten query, a truncated table, a missing schema table, an exception
quietly converted into a default. Traces that record the actual intermediate
inputs are what found them.

**Stale deployments (Bugs 2, 5, 15).** Three times, the code being tested was
not the code running. Check what's deployed before diagnosing what's broken.

**Hardcoded assumptions (Bugs 8, 17, 19).** Each database switch exposed
assumptions baked into lists or prompts. Reading structure from the live
schema holds up; hand-maintained keyword lists and prompts describing one
fixed domain don't.

**Evaluation blind spots (Bugs 10, 17).** The judge was miscalibrated and the
benchmark missed a question on the demo's own front page. An evaluation is
only as good as its coverage of what users actually do.

**No monitoring (Bug 14).** Nothing noticed a dead demo for 77 days. A system
that can break silently eventually will.