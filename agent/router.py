"""
agent/router.py
---------------
Phase 7 — Agentic Router

LLM-based router that decides which retrieval path to take:
    - SQL:  question needs structured data from the database
    - RAG:  question needs information from industry documents
    - BOTH: question needs data from both sources

The router uses GPT-4o to reason about the question type,
available schema, and document corpus before deciding.

This is the core of the agentic system — the agent reasons
about which tool(s) to use rather than following fixed rules.
"""

import os
import json
from pathlib import Path

from dotenv import load_dotenv
load_dotenv(Path(__file__).parent.parent / ".env")

from openai import OpenAI
from enum import Enum


# ------------------------------------------------------------------
# Route types
# ------------------------------------------------------------------

class Route(str, Enum):
    SQL  = "SQL"   # structured data query → database
    RAG  = "RAG"   # unstructured knowledge → documents
    BOTH = "BOTH"  # needs both sources


# ------------------------------------------------------------------
# Router prompt
# ------------------------------------------------------------------

ROUTER_SYSTEM_PROMPT = """You are an intelligent query router for an AI agent that has access to two data sources:

1. **SQL DATABASE (Chinook Music Store)**
   Tables: Album, Artist, Customer, Employee, Genre, Invoice, InvoiceLine, MediaType, Playlist, PlaylistTrack, Track
   Contains: Sales transactions, customer records, music catalog, invoice data, employee info
   Good for: counts, totals, rankings, trends from the actual business data, specific customer/artist/track lookups

2. **DOCUMENT CORPUS (Music Industry Reports)**
   Documents:
   - IFPI Global Music Report 2025 & 2026 (global industry revenue, streaming trends, regional growth)
   - Spotify Annual Report 20-F (Spotify financials, MAU counts, subscriber data, business strategy)
   - Luminate 2025 Year-End Music Report (US music consumption, genre trends, listener behavior)
   Good for: industry context, market trends, streaming statistics, business strategy, policy questions

Your job is to decide the best retrieval route for each question.

ROUTING RULES:
- SQL: Question asks about specific data IN the Chinook database (our artists, our customers, our sales, our tracks)
- RAG: Question asks about industry knowledge, market trends, external statistics, or business context NOT in the database
- BOTH: Question combines internal data with external context (e.g. "How does our Rock revenue compare to industry trends?")

IMPORTANT:
- Questions with "our", "we", "total", "how many", "which customer/artist/track" → likely SQL
- Questions with "industry", "global", "Spotify", "streaming market", "why", "what does the report say" → likely RAG  
- Questions comparing internal data to external benchmarks → BOTH

Respond ONLY with valid JSON in this exact format:
{
  "route": "SQL" | "RAG" | "BOTH",
  "reason": "brief explanation of why",
  "sql_focus": "what SQL should find (if applicable, else null)",
  "rag_focus": "what documents should find (if applicable, else null)"
}"""

ROUTER_USER_TEMPLATE = """Question: {question}

Decide the routing for this question."""


# ------------------------------------------------------------------
# Router function
# ------------------------------------------------------------------

_openai_client = None

def _get_client():
    global _openai_client
    if _openai_client is None:
        _openai_client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
    return _openai_client


def route_question(question: str) -> dict:
    # Fast path: obvious SQL questions skip LLM router
    q = question.lower()
    sql_signals = ["how many", "count", "total", "list all", "show me all", "average"]
    rag_signals = ["ifpi", "spotify", "report", "industry", "global", "worldwide", "according to"]
    
    if any(s in q for s in rag_signals):
        pass  # needs LLM router
    elif any(s in q for s in sql_signals) and not any(s in q for s in ["compare", "trend", "industry"]):
        # Fast SQL route — no LLM call needed
        return {
            "route":     Route.SQL,
            "reason":    "Fast path: clear SQL question",
            "sql_focus": question,
            "rag_focus": None,
        }
    
    # Slow path: ambiguous questions need LLM router
    client = _get_client()

    try:
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": ROUTER_SYSTEM_PROMPT},
                {"role": "user",   "content": ROUTER_USER_TEMPLATE.format(question=question)},
            ],
            temperature=0,        # deterministic routing
            max_tokens=300,
            response_format={"type": "json_object"},
        )

        raw = response.choices[0].message.content
        result = json.loads(raw)

        # Validate and normalize
        route_str = result.get("route", "SQL").upper()
        if route_str not in ("SQL", "RAG", "BOTH"):
            route_str = "SQL"  # safe default

        return {
            "route":     Route(route_str),
            "reason":    result.get("reason", ""),
            "sql_focus": result.get("sql_focus"),
            "rag_focus": result.get("rag_focus"),
        }

    except json.JSONDecodeError as e:
        print(f"[Router] JSON parse error: {e}")
        return {"route": Route.SQL, "reason": "Parse error — defaulting to SQL", "sql_focus": question, "rag_focus": None}

    except Exception as e:
        print(f"[Router] Error: {e}")
        return {"route": Route.SQL, "reason": f"Error — defaulting to SQL: {e}", "sql_focus": question, "rag_focus": None}


# ------------------------------------------------------------------
# Quick test
# ------------------------------------------------------------------

if __name__ == "__main__":
    test_questions = [
        # SQL questions
        "How many orders do we have in total?",
        "Who are the top 5 customers by revenue?",
        "Which artist has the most albums in our catalog?",
        "What is the total revenue by genre?",

        # RAG questions
        "What is the global recorded music revenue growth rate?",
        "How many paid streaming subscribers are there worldwide?",
        "What does the IFPI report say about Latin America?",
        "What is Spotify's monthly active user count?",

        # BOTH questions
        "How does our Rock genre revenue compare to global industry trends?",
        "Which region should we expand to based on our customer data and industry reports?",
        "How does our Latin music sales compare to the global Latin market growth?",
        "Why is streaming dominating music revenue and how does our catalog reflect this?",
    ]

    print("=" * 60)
    print("ROUTER TEST")
    print("=" * 60)

    sql_count = rag_count = both_count = 0

    for question in test_questions:
        result = route_question(question)
        route = result["route"]

        if route == Route.SQL:
            sql_count += 1
            icon = "🗄️ "
        elif route == Route.RAG:
            rag_count += 1
            icon = "📄"
        else:
            both_count += 1
            icon = "🔀"

        print(f"\n{icon} [{route.value}] {question}")
        print(f"   Reason: {result['reason']}")
        if result["sql_focus"]:
            print(f"   SQL: {result['sql_focus']}")
        if result["rag_focus"]:
            print(f"   RAG: {result['rag_focus']}")

    print(f"\n{'='*60}")
    print(f"ROUTING SUMMARY")
    print(f"  SQL:  {sql_count} questions")
    print(f"  RAG:  {rag_count} questions")
    print(f"  BOTH: {both_count} questions")
    print(f"{'='*60}")
