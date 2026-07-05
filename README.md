# nl-db-agent — Agentic RAG for Natural Language Database Queries

> A production-grade agentic system that routes natural language questions to SQL, industry documents, or both — then synthesizes a grounded answer with source citations.

![Python](https://img.shields.io/badge/Python-3.11-blue)
![LangGraph](https://img.shields.io/badge/LangGraph-0.2-green)
![FastAPI](https://img.shields.io/badge/FastAPI-0.110-teal)
![Accuracy](https://img.shields.io/badge/Accuracy-86.1%25-brightgreen)
![Hallucination](https://img.shields.io/badge/Hallucination-0%25_SQL-brightgreen)
![License](https://img.shields.io/badge/License-MIT-yellow)

**Live Demo:** https://llm-sql-agent-ui.vercel.app

**Live API:** https://nl-db-agent-140623834959.us-central1.run.app/docs

---

## The Problem

Business analysts need two kinds of answers: what's happening in *our* data, and what's happening in the *industry*. Today they file tickets for the first and read PDFs for the second. Neither is fast enough.

> *"Last month I watched someone spend 45 minutes trying to get a single number — then another hour searching through an IFPI report to contextualize it."*

This project routes both questions to the right source automatically.

---

## What It Does

Ask any question in plain English. The agent decides whether to query the database, search industry documents, or combine both.

```
SQL route:
"What is the total revenue by genre?"
→ Rock leads with $826.65, followed by Latin at $382.14 and Metal at $261.36

RAG route:
"What is the global recorded music revenue growth rate?"
→ +4.8% in 2024 (IFPI Global Music Report 2025, p.4)

BOTH route:
"How does our Rock revenue compare to global industry trends?"
→ Our Chinook data shows Rock at $826.65. The IFPI 2026 report confirms
  Rock maintains ~34% global market share, suggesting our catalog aligns
  with industry demand.
```

---

## Architecture

```
User Question
      ↓
┌─────────────────────────────────────────────────────────────────┐
│                      LangGraph Agent (7 nodes)                   │
│                                                                   │
│  [Clarify] → [Router] ──────────────────────────────────────┐   │
│                  │                                           │   │
│         ┌────────┼────────┐                                  │   │
│         ↓        ↓        ↓                                  │   │
│      SQL path  RAG path  BOTH path                           │   │
│         │        │     (SQL + RAG)                           │   │
│    [Schema]   [RAG]    [Schema + RAG]                        │   │
│    [Generate]   │      [Generate]                            │   │
│    [Validate]   │      [Validate]                            │   │
│    [Guardrails] │      [Guardrails]                          │   │
│    [Execute]    │      [Execute]                             │   │
│         └────────┘──────────┘                                │   │
│                  ↓                                           │   │
│              [Format] ───────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────┘
      ↓
Plain English Answer + Source Citations + Route Explanation
```

### Agent Nodes

| Node | Responsibility |
|------|---------------|
| Clarify | Detects ambiguous questions, asks for clarification |
| Router | LLM classifies question as SQL / RAG / BOTH |
| RAG | Pinecone semantic search across 4 industry documents |
| Schema | Retrieves relevant Chinook tables before SQL generation |
| Generate | GPT-4o-mini converts question → SQL (temperature=0) |
| Validate | Syntax + safety + complexity check |
| Guardrails | RBAC + rate limits + injection detection |
| Execute | Read-only, row-capped, timeout-protected execution |
| Format | Synthesizes SQL results + RAG context into plain English |

### RAG Pipeline

```
4 Industry Documents → PDF chunking → text-embedding-3-small
→ Pinecone (index: doc-intelligence, namespace: chinook-music)
→ 2,462 vectors total

Documents:
  - IFPI Global Music Report 2025 (151 vectors)
  - IFPI Global Music Report 2026 (195 vectors)
  - Spotify Annual Report 20-F   (1,960 vectors)
  - Luminate 2025 Year-End Report (156 vectors)
```

### Human-Centered AI Features

**Explainability Panel** — Every answer shows "Why this route?" revealing the agent's routing decision and reasoning. Users can verify whether the agent used their database, industry documents, or both.

**Human Feedback Loop** — Thumbs up/down on every answer. Feedback stored via `/feedback` endpoint and aggregated via `/feedback/summary`. Creates a human baseline for calibrating LLM-as-judge scores — directly mirrors RLHF data collection.

---

## Evaluation

Benchmarked against 36 queries across 6 tiers using deterministic checks + LLM-as-judge.

```
Overall accuracy   : 86.1%  (31/36)
SQL hallucination  : 0%
Avg latency        : ~6,500ms (Bangladesh → US, GPT-4o-mini)
LLM Judge avg      : SQL 3.8/5 · BOTH 2.9/5

By Tier:
  Easy           : 100%  ██████████  (8/8)
  Medium         : 100%  ██████████  (8/8)
  Hard           : 100%  ██████████  (6/6)
  RAG            :  83%  ████████    (5/6)
  BOTH           :  60%  ██████      (3/5)
  Clarification  :  67%  ██████      (2/3)
```

### LLM-as-Judge Framework

Each answer scored on route-specific dimensions:

| Route | Dimensions |
|-------|-----------|
| SQL | Correctness, Hallucination, Helpfulness |
| RAG | Faithfulness, Source Attribution, Relevance, Helpfulness |
| BOTH | Correctness, Faithfulness, Synthesis Quality, Helpfulness |

**Key finding:** LLM-as-judge produces false positive hallucination flags when `rag_context` is truncated (500 chars insufficient; fixed to 3,000+ chars). Production implication: corrupts RLHF training signals if not caught. Documented in `MISTAKES.md`.

---

## Tech Stack

| Layer | Technology |
|-------|-----------|
| Agent framework | LangGraph 0.2 |
| LLM — routing + format | GPT-4o-mini (temperature=0) |
| LLM — SQL generation | GPT-4o-mini (temperature=0) |
| LLM — evaluation judge | GPT-4o |
| Vector database | Pinecone (text-embedding-3-small) |
| Demo database | Chinook SQLite (11 tables, music store) |
| Backend API | FastAPI on Google Cloud Run |
| Frontend | Next.js on Vercel |
| Observability | Custom JSON tracer + LLM-as-judge eval |

---

## Project Structure

```
nl-db-agent/
├── agent/
│   ├── graph.py          # LangGraph state machine (7 nodes, 3 routes)
│   ├── router.py         # LLM-based SQL/RAG/BOTH router with fast path
│   ├── state.py          # AgentState schema with RAG fields
│   └── run_agent.py      # CLI test runner
├── rag/
│   ├── ingestion.py      # PDF → chunks → embeddings → Pinecone
│   └── retriever.py      # Semantic search, top_k=5, min_score=0.3
├── tools/
│   ├── schema_inspector.py   # Schema retrieval + table keyword mapping
│   ├── sql_generator.py      # GPT-4o-mini SQL generation
│   ├── validator.py          # Syntax + complexity validation
│   ├── executor.py           # Read-only, 100-row cap, 10s timeout
│   ├── clarifier.py          # Ambiguity detection
│   └── formatter.py          # Response synthesis (SQL + RAG)
├── guardrails/
│   ├── permissions.py    # Dynamic RBAC — column-based sensitivity
│   ├── limits.py         # Rate limits + resource caps
│   └── safety.py         # Injection + exfiltration detection
├── evaluation/
│   ├── evaluation_dataset.json  # 36 benchmark queries (6 tiers)
│   ├── llm_judge.py             # LLM-as-judge (SQL/RAG/BOTH prompts)
│   ├── metrics.py               # Evaluation runner with --tier + --judge
│   └── eval_results.json        # Latest benchmark results
├── observability/
│   ├── tracer.py         # JSON trace logger
│   └── feedback.jsonl    # Human feedback log (thumbs up/down)
├── api/
│   └── main.py           # FastAPI: /query, /feedback, /feedback/summary
├── db/
│   └── chinook.db        # Chinook SQLite (music store demo)
├── documents/            # Source PDFs (local only — vectors in Pinecone)
├── MISTAKES.md           # Phase-by-phase bug documentation
└── requirements.txt
```

---

## Quick Start

### 1. Clone and install

```bash
git clone https://github.com/SAMithila/nl-db-agent.git
cd nl-db-agent
pip install -r requirements.txt
```

### 2. Set up environment

```bash
cp .env.example .env
# Add OPENAI_API_KEY and PINECONE_API_KEY to .env
```

### 3. Run the agent (terminal)

```bash
python -c "
from agent.graph import run_query
s = run_query('What is the total revenue by genre?')
print(s.final_response['summary'])
"
```

### 4. Run the API locally

```bash
uvicorn api.main:app --reload --port 8000
```

### 5. Run evaluation

```bash
# SQL tiers only (fast)
python -m evaluation.metrics --tier sql

# RAG tier with LLM judge
python -m evaluation.metrics --tier rag --judge

# Full benchmark
python -m evaluation.metrics --tier all --judge
```

---

## Key Engineering Decisions

**Why LangGraph over a simple chain?**
LangGraph gives explicit state machine control — every node, every transition, every routing decision is visible and auditable. The three-route architecture (SQL / RAG / BOTH) requires conditional branching that chains cannot express cleanly.

**Why a fast-path router?**
The LLM router adds ~500ms per query. Questions with clear SQL signals ("how many", "total", "count") bypass the LLM router entirely via keyword matching — saving the API call for ambiguous cases that actually need it.

**Why dynamic RBAC instead of hardcoded table names?**
The permissions layer inspects the connected database schema at runtime using SQLAlchemy `inspect()`. This means the system works with any database without code changes — a requirement for a general-purpose agent.

**Why LLM-as-judge for RAG evaluation?**
Keyword matching alone cannot evaluate answer quality for open-ended document retrieval questions. LLM-as-judge scores faithfulness, source attribution, and hallucination — but requires sufficient context (3,000+ chars) to avoid false positive hallucination flags.

**Why human feedback alongside LLM-as-judge?**
LLM judges have known biases and calibration issues. Human thumbs up/down ratings provide a ground truth baseline to validate judge scores — the same pattern used in RLHF pipelines.

---

## Real Bugs Caught

See `MISTAKES.md` for full phase-by-phase documentation.

**Bug: LLM-as-judge false positives from truncated context**
Judge scored correct RAG answers as hallucinations because `rag_context` was truncated to 500 chars — the cited fact appeared beyond the cutoff. Fix: pass 3,000+ chars. Production implication: truncated context corrupts RLHF training signals.

**Bug: RAG route returning `success: False`**
`api/main.py` used `state.execution_success` to determine response success. For RAG-only routes, no SQL executes, so `execution_success` is always `False`. Fix: check `final_response.get("success")` instead.

**Bug: Router corruption from paste error**
Correct `_format_rag_response` code was accidentally pasted inside `route_question()` in `router.py`. The `answer` variable didn't exist in scope, causing a `NameError` on every routing call. Fix: remove corrupted block, restore correct return dict.

---

## About

Built as part of a production-grade AI/ML portfolio targeting FAANG-level roles.

**Portfolio:** github.com/SAMithila
**Live demo:** https://llm-sql-agent-ui.vercel.app