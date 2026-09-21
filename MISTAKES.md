# MISTAKES.md

## Bug 1: LLM hallucinating view columns
**Phase 4** 

**Symptom:** "What is the total revenue by product category?" failed 3 retries

**Root cause:** LLM treated `order_revenue` view as a base table and tried to
access `oi.unit_price` and `oi.product_id` which don't exist in the view

**Fix:**
1. Added `order_items` to revenue keyword mapping in schema_inspector
2. Added explicit view column descriptions to sql_generator system prompt

**Lesson:** Views must be described with their actual columns in the schema
context — the LLM cannot infer view structure from the name alone


## Eval Run 1 — Results
- Overall: 96% (24/25)
- Easy: 100%, Medium: 100%, Hard: 100%
- Clarification: 66.7% (2/3)
- Failure: C003 "Which products are doing well?"
  - Root cause: vague metric pattern not in keyword list
  - Fix: added "doing well" to AMBIGUOUS_PATTERNS
- Avg latency: 4580ms (GPT-4o API)
- Retry rate: 0%


## Bug 2: SQLite file upload endpoint missing from deployment
**Phase 6** (Cloud Run Deployment)

**Symptom:** `/connect/sqlite-upload` returned 404 on Cloud Run despite
working locally. Browser showed "Network Error" on file upload.

**Root cause:** `git push` does not redeploy Cloud Run. The new endpoint
existed in code but the running container was built from an older image.
Cloud Run requires an explicit `gcloud run deploy --source .` command to
rebuild and deploy.

**Fix:** Always run after any backend change:
```bash
gcloud run deploy nl-db-agent \
  --source . \
  --region us-central1 \
  --allow-unauthenticated
```

**Lesson:** Cloud Run is not like Vercel — it does not watch GitHub for
changes. Every backend deploy requires an explicit gcloud command.
This caused 4 separate "stale container" incidents in this session.


## Bug 3: Missing imports caused container startup failure
**Phase 6** (Cloud Run Deployment)

**Symptom:** Cloud Run revision failed to start with:
`NameError: name 'File' is not defined`

**Root cause:** `UploadFile`, `File`, `Form`, `tempfile`, `shutil`, `sqlite3`
were used in the new `/connect/sqlite-upload` endpoint but not imported.
The container built successfully (no compile-time check) but crashed at
runtime when Python tried to parse the module.

**Fix:** Added to top of `api/main.py`:
```python
from fastapi import FastAPI, HTTPException, Header, UploadFile, File, Form
import tempfile, shutil, sqlite3
```

**Lesson:** Python import errors only surface at runtime, not build time.
Always run `python3 -c "import ast; ast.parse(open('api/main.py').read())"` 
locally before deploying to catch syntax errors early.


## Bug 4: Syntax error from inline sed edit
**Phase 6** (Cloud Run Deployment)

**Symptom:** Container failed to start with `SyntaxError: invalid syntax`
on the sqlite_master query line.

**Root cause:** A `sed` command meant to update one line accidentally merged
two lines into one:
```python
# Broken — two statements on one line
cursor.execute("SELECT name FROM sqlite_master...")        tables = [row[0]...]
```

**Fix:** Manually split into two properly indented lines in VS Code.

**Lesson:** Never use `sed` to edit Python code in production files.
Use VS Code or verify with `python3 -c "import ast; ast.parse(...)"` after
any terminal-based file edit.


## Bug 5: CORS blocked on multipart upload endpoint
**Phase 7** (UI deployment)

**Symptom:** Browser console showed CORS error on `/connect/sqlite-upload`
even though other endpoints worked fine.

**Root cause:** Two separate issues compounding each other:
1. The deployed container was stale (Bug 2 again) — CORS fix wasn't live
2. axios was sending `"Content-Type": "multipart/form-data"` manually,
   which omits the required `boundary` parameter. FastAPI rejected the
   malformed request, and the browser misread the failed response as a
   CORS error.

**Fix:**
1. Redeploy backend with `gcloud run deploy --source .`
2. Remove manual Content-Type header — let axios set it automatically:
```typescript
// Wrong — missing boundary
const res = await axios.post(url, formData, {
  headers: { "Content-Type": "multipart/form-data" },
});

// Correct — axios adds boundary automatically
const res = await axios.post(url, formData);
```

**Lesson:** Never manually set Content-Type for multipart/form-data.
The browser/axios must generate the boundary string automatically.
A failed API request can masquerade as a CORS error in the browser console.


## Bug 6: React hydration mismatch (Error #418)
**Phase 7** (UI deployment)

**Symptom:** Console showed "Uncaught Error: Minified React error #418"
(Hydration Mismatch). UI still rendered but with warnings.

**Root cause:** `localStorage` was read inside `useState` initializer
functions, which run during Next.js server-side rendering where `window`
is undefined. Server rendered with fallback values, client rendered with
localStorage values — mismatch.

**Fix:** Moved all localStorage reads into a `useEffect` with a `hydrated`
gate:
```typescript
const [hydrated, setHydrated] = useState(false);

useEffect(() => {
  setDark(loadFromStorage("llm_sql_dark", false));
  setConnection(loadFromStorage("llm_sql_connection", { connected: false }));
  // ... etc
  setHydrated(true);
}, []);

if (!hydrated) return null; // after ALL hooks
```

**Lesson:** Never read `localStorage`, `window`, or `document` inside
`useState` initializers in Next.js. Always use `useEffect` for
browser-only APIs. The `if (!hydrated) return null` must come AFTER
all hook declarations — React hooks cannot appear after a conditional return.


## Bug 7: /disconnect endpoint missing from deployed API
**Phase 6:** (Cloud Run Deployment)

**Symptom:** Browser console showed 404 on `/disconnect/{session_id}`.
Frontend called disconnect on page load to validate stale sessions,
causing a visible error.

**Root cause:** The `disconnect` function was imported from `db_connector`
but no FastAPI route was ever defined for it in `main.py`.

**Fix:** Added the missing route:
```python
@app.post("/disconnect/{session_id}")
def disconnect_database(session_id: str = "default"):
    disconnect(session_id)
    return {"success": True, "message": "Disconnected"}
```

**Lesson:** Importing a function is not the same as exposing it as an
endpoint. Always verify routes are registered by checking `/openapi.json`
after deployment.


## Architecture Decision: File Upload Size Limit
**Phase 6:** (UI deployment)

**Problem:** Direct multipart POST to Cloud Run hits the 32MB request body
limit. A 135MB SQLite file failed silently — the frontend showed
"Connecting..." indefinitely.

**Current state:** Works for files under ~30MB (dev.db at 70KB works fine).

**Production fix:** Use GCS signed URLs for direct browser-to-storage upload,
bypassing Cloud Run entirely:

## Bug 8: Northwind hardcoded in schema_inspector and validator
**Phase 7** — Agentic RAG

**Symptom:** After switching to Chinook, queries failed with
"no such table: Genre". SQL generated correctly but validation
failed against wrong database.

**Root cause:** Two files had Northwind hardcoded:
- tools/validator.py: DB_PATH = "../db/dev.db"  
- tools/schema_inspector.py: TABLE_KEYWORDS with Northwind table names
- guardrails/permissions.py: allowed_tables with Northwind table names

**Fix:** 
1. sed to update DB_PATH to chinook.db in validator.py
2. Updated TABLE_KEYWORDS to Chinook tables in schema_inspector.py
3. Rewrote permissions.py with dynamic column-based detection

**Lesson:** Never hardcode table names in guardrails or schema
mapping. Use dynamic inspection via SQLAlchemy inspect() so the
system works with any database.


## Bug 9: RAG route returning success=False
**Phase 7:** — Agentic RAG

**Root cause:** Two separate issues:
1. _format_rag_response() missing "success": True in return dict
2. api/main.py used state.execution_success to determine success,
   which is always False for RAG-only routes (no SQL executed)

**Fix:**
1. Added "success": True to both returns in _format_rag_response
2. Changed main.py to use final_response.get("success") instead
   of state.execution_success

**Lesson:** Each route (SQL/RAG/BOTH) has different success
signals. The API layer must check the final_response dict,
not intermediate pipeline state flags.

## Bug 10: LLM-as-judge false positives from insufficient context
**Phase 7C** — Evaluation

**Symptom:** Judge scored correct RAG answers as hallucinations.
R001 answer "+4.8% growth" correctly cited from IFPI GMR 2025,
but judge gave faithfulness=1 and hallucination=true.

**Root cause:** rag_context passed to judge was truncated to 500
characters. The +4.8% figure appeared in a chunk beyond that
cutoff, so the judge couldn't verify the claim and assumed
hallucination.

**Fix:** Pass full rag_context (3000+ chars) to judge. In
production, pass the complete retrieved chunks as separate
messages in the judge prompt rather than concatenated text.

**Production implications:**
- False hallucination flags corrupt RLHF training signals
- False positives in monitoring waste engineering time
- Deployment gates based on flawed metrics block good answers

**Lesson:** LLM-as-judge requires sufficient context to verify
factual claims. Always validate judge calibration against
human-labeled ground truth before using scores as training signal
or deployment gates. 

## Bug 11: Router corrupted by accidental paste
**Phase:** 7 — Agentic RAG

**Symptom:** Every query returned "Route: SQL — Error — defaulting to SQL:
name 'answer' is not defined" in the trace. All three routes fell back to SQL.

**Root cause:** While adding "success": True to _format_rag_response() in
graph.py, the return dict was accidentally pasted inside route_question() in
router.py. The variable `answer` doesn't exist in router scope, causing a
NameError on every routing call.

**Fix:** Removed the corrupted 6-line block from router.py and restored the
correct return dict:
```python
return {
    "route":     Route(route_str),
    "reason":    result.get("reason", ""),
    "sql_focus": result.get("sql_focus"),
    "rag_focus": result.get("rag_focus"),
}
```

**Lesson:** When editing multiple files simultaneously, verify each file
independently before committing. A grep for the variable name would have
caught this instantly: `grep -n "answer" agent/router.py`

## Bug 12: Router query rewriting silently degraded RAG retrieval

**Symptom:** RAG-route questions returned "the documents do not specify..."
even when the fact was in the corpus. Retrieval reported success and cited
the correct source documents.

**Cause:** `rag_node` used `search_query = state.rag_focus or state.question`.
The router set `rag_focus` to a document title ("IFPI Global Music Report
2025 & 2026"), so the embedding search ran against a title instead of the
user's question. It returned report boilerplate; the answer chunk ranked
outside top_k.

**Why it was hard to find:** every layer downstream behaved correctly on
the wrong input. Retrieval succeeded, sources were attached, no error was
raised, and the model's refusal was the correct response to what it was
given. I diagnosed it as context truncation, then as a context-passing bug
on the RAG-only branch, then as prompt over-steering, before checking the
trace line showing the actual embedding query.

**Fix:** `search_query = f"{question} {rag_focus}".strip() if rag_focus
else question`. The user's words lead; the router hint is additive.

**Production implication:** any rewriting between user input and retrieval
is a silent failure surface. Log the actual embedding query, not just the
user's question — the trace line is what eventually identified this.

---

## Bug 13: Anti-hallucination prompt over-steered into refusal

**Symptom:** even with correct context in the prompt, the RAG formatter
declined to answer.

**Cause:** the system prompt said "Answer based ONLY on the provided
document context" and "If the context doesn't contain enough information,
say so clearly." Together these bias toward refusal. Compounding it, PDF
extraction split decimals ("4. 8%"), making the figure harder to recognise
as an answer.

**Fix:** removed "ONLY", noted the split-decimal artifact explicitly in the
prompt, and made the refusal conditional ("only after genuinely searching").

**Note:** this was a real improvement but it was fixing a symptom. The root
cause was Bug 12.

**Pairs with the LLM-judge truncation bug:** that one produced false
positive hallucination flags; this one produced false negative answers.
Both are grounding failures in opposite directions, and both are invisible
in aggregate accuracy — the system looks like it is working.

---

## Bug 14: Cloud Run died silently when trial billing lapsed

**Symptom:** the public demo returned "API connection failed". Discovered 77
days after it broke.

**Cause:** Google Cloud trial credits expired, billing account closed,
Cloud Run refused to start the container. No application logs were produced
because the container never ran — which made it look like a code fault.

**Fix:** migrated the backend to Render (free tier, no card required).

**Production implication:** a portfolio demo with no health check is a
liability. The README advertised a link that had been dead for two and a
half months. Next: `/health` endpoint plus a daily scheduled check.

---

## Bug 15: Malformed requirements.txt blocked deploys while I debugged
against stale production code

**Symptom:** fixes verified locally did not change production behaviour.

**Cause:** `echo "python-multipart" >> requirements.txt` appended without a
trailing newline, producing `pypdf==5.1.0python-multipart`. Every deploy
failed at pip install in ~20s. Production stayed on the last good commit.

**Why it wasted time:** the symptom (fix appears not to work) pointed at the
fix, not at the pipeline. Two rounds of re-diagnosis before checking the
deploy status.

**Lesson:** verify the deploy landed before re-testing in production. The
trace timestamp and commit SHA should be the first thing checked, not the last.

## Bug 16: Formatter summarised the first 10 rows as if they were all of them

**Symptom:** "What is the total revenue by genre?" answered "Alternative & Punk
generated the highest revenue at $241.56." The chart directly below showed Rock
at $826.65. Data correct, chart correct, text wrong.

**Cause:** `formatter.py` passed `rows[:10]` to the LLM. The generated SQL had
GROUP BY but no ORDER BY, so rows returned alphabetically and Rock was row 18.
The model saw 10 of 24 genres and reported the maximum of that subset as the
overall maximum. Likely also the cause of "top 5 artists all at $1.98."

**Fix:** preview up to 50 rows. The executor already caps results at 100.

**Why it matters:** the only wrong thing on screen was the sentence the user
reads. It was fluent, specific, and false, with a real number attached.

**Better fix (not yet done):** compute aggregates — max, min, totals — in
Python and hand them to the model as facts. Arithmetic over a table should
not be delegated to a language model.

## Bug 17: Schema context omitted bridge tables; model invented a join

**Symptom:** "Who are our top 5 artists by revenue?" — a sidebar example
question — failed with `no such column: t.ArtistId`. The error shown to the
user was generic: "Query execution failed". The real SQLite message was
discarded.

**Cause:** table selection is keyword-based. The question matched Artist,
Invoice and InvoiceLine, but not Track or Album, which are the tables that
connect them. With no bridge in context, the model guessed a direct
Track→Artist join that doesn't exist in Chinook. The genre question had the
same gap (Track missing) but worked because the model remembered Chinook's
structure.

**Fix:** after keyword matching, walk the foreign-key graph and add the
tables on the shortest join path between those selected. Uses FK data
already collected by `get_schema()`. Works on any database. Result: 10/10
successful runs, down from failing.

**What the eval missed:** the Hard tier reports 100%, but this question
isn't in the evaluation set. The example questions a visitor is most likely
to click should be in the benchmark.

**Harness lesson:** an intermediate test re-executed the generated SQL
itself instead of reading the agent's own success flag, and its results
disagreed with the agent's. Measure the system's output, not a
reconstruction of it.

**Known limitation:** FK expansion only connects tables that keyword
matching already chose. "Top selling products" still misses Track, because
"product" matches no keyword. TABLE_KEYWORDS remains hardcoded to Chinook.