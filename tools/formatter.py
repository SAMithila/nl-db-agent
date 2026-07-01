"""
formatter.py
------------
Tool 6 — Response Formatter

Takes raw query results and converts them into clear,
plain English responses that non-technical users can understand.

Phase 7 update: Added rag_context and rag_sources parameters
to format_response() for the BOTH route — SQL results get
enriched with document context before the final answer.

Functions:
    format_response()    → convert raw results to plain English
    format_error()       → convert errors to friendly messages
    format_no_results()  → handle empty result sets gracefully
"""

import os
import json
import re
from pathlib import Path
from typing import Optional
from openai import OpenAI
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")

client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
MODEL = "gpt-4o-mini"  # was "gpt-4o"

# ------------------------------------------------------------------
# Tool 6A: format_response()
# ------------------------------------------------------------------

def format_response(
    question:         str,
    sql:              str,
    columns:          list,
    rows:             list,
    execution_ms:     int,
    truncated:        bool            = False,
    explanation:      str             = "",
    assumptions:      str             = "",
    rag_context:      Optional[str]   = None,   # Phase 7: document context
    rag_sources:      Optional[list]  = None,   # Phase 7: source citations
) -> dict:
    """
    Converts raw query results into a plain English response.
    When rag_context is provided (BOTH route), enriches the answer
    with industry document context alongside the SQL data.

    Args:
        question:     The original user question.
        sql:          The SQL that was executed.
        columns:      Column names from the result.
        rows:         Data rows from the result.
        execution_ms: Query execution time in milliseconds.
        truncated:    Whether results were capped at row limit.
        explanation:  SQL explanation from sql_generator.
        assumptions:  Assumptions made during SQL generation.
        rag_context:  Document context from Pinecone (BOTH route only).
        rag_sources:  List of source documents cited (BOTH route only).

    Returns:
        dict with:
        {
            "success":       True,
            "summary":       "Plain English answer",
            "key_insights":  ["Insight 1", "Insight 2"],
            "data_table":    {"columns": [...], "rows": [...]},
            "sources":       [...],   # only for BOTH route
            "metadata":      {"execution_ms": 42, "row_count": 5}
        }
    """

    # Handle empty results
    if not rows:
        return format_no_results(question)

    # Build data preview for LLM (max 10 rows)
    preview_rows = rows[:10]
    data_preview = _build_data_preview(columns, preview_rows)

    # Choose prompt based on whether RAG context is available
    if rag_context:
        prompt = _build_both_prompt(
            question, data_preview, rows, truncated,
            assumptions, rag_context
        )
    else:
        prompt = _build_sql_only_prompt(
            question, data_preview, rows, truncated, assumptions
        )

    try:
        response = client.chat.completions.create(
            model=MODEL,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.3,
            max_tokens=600,
        )

        raw     = response.choices[0].message.content.strip()
        cleaned = re.sub(r"```(?:json)?", "", raw).strip().strip("`").strip()
        data    = json.loads(cleaned)

        result = {
            "success":      True,
            "summary":      data.get("summary", ""),
            "key_insights": data.get("key_insights", []),
            "data_table":   {"columns": columns, "rows": rows},
            "metadata": {
                "execution_ms": execution_ms,
                "row_count":    len(rows),
                "truncated":    truncated,
                "sql":          sql,
                "route":        "BOTH" if rag_context else "SQL",
            },
        }

        # Add sources for BOTH route
        if rag_sources:
            result["sources"] = rag_sources

        return result

    except Exception as e:
        return {
            "success":      True,
            "summary":      f"Query returned {len(rows)} result(s).",
            "key_insights": [],
            "data_table":   {"columns": columns, "rows": rows},
            "metadata": {
                "execution_ms": execution_ms,
                "row_count":    len(rows),
                "truncated":    truncated,
                "sql":          sql,
                "warning":      f"Summary generation failed: {str(e)}",
                "route":        "BOTH" if rag_context else "SQL",
            },
        }


# ------------------------------------------------------------------
# Prompt builders
# ------------------------------------------------------------------

def _build_sql_only_prompt(
    question: str,
    data_preview: str,
    rows: list,
    truncated: bool,
    assumptions: str,
) -> str:
    """Standard SQL-only prompt (unchanged from Phase 1-6)."""
    return f"""A user asked this business question:
"{question}"

The database returned these results:
{data_preview}

Total rows returned: {len(rows)}
{"Note: Results were truncated at 100 rows maximum." if truncated else ""}
{"SQL assumption: " + assumptions if assumptions else ""}

Write a clear, concise business response that:
1. Directly answers their question in 1-2 sentences
2. Highlights 2-3 key insights from the data
3. Uses specific numbers from the results
4. Sounds like a helpful data analyst, not a robot

Respond ONLY with valid JSON in this exact format:
{{
    "summary": "Direct answer to their question (1-2 sentences with key numbers)",
    "key_insights": [
        "Specific insight 1 with actual numbers",
        "Specific insight 2 with actual numbers"
    ]
}}

Nothing else. No markdown. Just the JSON."""


def _build_both_prompt(
    question: str,
    data_preview: str,
    rows: list,
    truncated: bool,
    assumptions: str,
    rag_context: str,
) -> str:
    """
    BOTH route prompt — combines SQL data with document context.
    The LLM synthesizes a richer answer that references both sources.
    """
    return f"""A user asked this business question:
"{question}"

You have TWO sources of information to answer this:

=== SOURCE 1: DATABASE RESULTS (Chinook Music Store) ===
{data_preview}

Total rows: {len(rows)}
{"Note: Results were truncated at 100 rows." if truncated else ""}
{"SQL assumption: " + assumptions if assumptions else ""}

=== SOURCE 2: INDUSTRY DOCUMENTS ===
{rag_context[:2000]}

Write a rich business response that:
1. Directly answers their question using BOTH the database data AND the industry context
2. Compares internal numbers to industry benchmarks where relevant
3. Highlights 3-4 key insights that combine both data sources
4. Clearly distinguishes what comes from "our data" vs "industry reports"
5. Sounds like a senior music industry analyst

Respond ONLY with valid JSON in this exact format:
{{
    "summary": "Answer combining internal data and industry context (2-3 sentences)",
    "key_insights": [
        "Insight from our database data with specific numbers",
        "Insight from industry reports with specific numbers",
        "Comparative insight combining both sources"
    ]
}}

Nothing else. No markdown. Just the JSON."""


# ------------------------------------------------------------------
# Tool 6B: format_error()
# ------------------------------------------------------------------

def format_error(question: str, error: str, stage: str) -> dict:
    """Converts a pipeline error into a friendly user message."""

    friendly_messages = {
        "validation":  "I couldn't safely generate a query for that question. Could you rephrase it?",
        "execution":   "The query ran into an issue. This might be a complex question — try breaking it into smaller parts.",
        "timeout":     "That query took too long to run. Try adding more specific filters.",
        "permission":  "You don't have access to that data. Please contact your administrator.",
        "generation":  "I had trouble understanding that question. Could you be more specific?",
        "rag":         "I couldn't find relevant information in the documents for that question.",
    }

    message = friendly_messages.get(
        stage,
        "Something went wrong. Please try rephrasing your question."
    )

    return {
        "success":       False,
        "error_message": message,
        "stage":         stage,
        "technical":     error,
    }


# ------------------------------------------------------------------
# Tool 6C: format_no_results()
# ------------------------------------------------------------------

def format_no_results(question: str) -> dict:
    """Handles the case where a valid query returns zero rows."""
    return {
        "success":      True,
        "summary":      "No results found for your question. The data may not exist for the criteria specified.",
        "key_insights": [
            "Try broadening your filters (wider date range, different category)",
            "Double-check the spelling of any names or IDs you specified",
        ],
        "data_table":   {"columns": [], "rows": []},
        "metadata":     {"row_count": 0},
    }


# ------------------------------------------------------------------
# Helper: build readable data preview for LLM prompt
# ------------------------------------------------------------------

def _build_data_preview(columns: list, rows: list) -> str:
    """Formats data as a simple text table for the LLM prompt."""
    if not rows:
        return "No data returned."

    header = " | ".join(str(col) for col in columns)
    lines  = [header, "-" * len(header)]

    for row in rows:
        lines.append(" | ".join(str(val) for val in row))

    return "\n".join(lines)


# ------------------------------------------------------------------
# Quick self-test
# ------------------------------------------------------------------

if __name__ == "__main__":

    print("=" * 60)
    print("FORMATTER TESTS")
    print("=" * 60)

    # Test 1: SQL-only
    print("\n── TEST 1: SQL only ──")
    result = format_response(
        question     = "What is the total revenue by genre?",
        sql          = "SELECT g.Name, SUM(il.UnitPrice * il.Quantity) FROM Genre g JOIN Track t...",
        columns      = ["Genre", "Revenue"],
        rows         = [
            ["Rock",   826.65],
            ["Latin",  382.14],
            ["Metal",  261.36],
        ],
        execution_ms = 3,
    )
    print(f"✅ Summary: {result['summary']}")
    for ins in result["key_insights"]:
        print(f"   • {ins}")

    # Test 2: BOTH route with RAG context
    print("\n── TEST 2: BOTH route (SQL + RAG) ──")
    result2 = format_response(
        question     = "How does our Rock revenue compare to global industry trends?",
        sql          = "SELECT g.Name, SUM(il.UnitPrice * il.Quantity) FROM Genre g...",
        columns      = ["Genre", "Revenue"],
        rows         = [["Rock", 826.65]],
        execution_ms = 4,
        rag_context  = "[Source 1: IFPI GMR 2026]\nRock remained the dominant genre globally, accounting for 28% of all streaming revenue in 2025. Latin music was the fastest-growing genre with 17% YoY growth.",
        rag_sources  = [{"title": "IFPI Global Music Report 2026", "publisher": "IFPI", "year": 2026}],
    )
    print(f"✅ Summary: {result2['summary']}")
    for ins in result2["key_insights"]:
        print(f"   • {ins}")
    print(f"   Sources: {result2.get('sources', [])}")

    # Test 3: Error
    print("\n── TEST 3: Error ──")
    err = format_error("Delete all orders", "Safety check failed", "validation")
    print(f"✅ Error: {err['error_message']}")

    print("\n" + "=" * 60)
    print("✅ All formatter tests complete")
    print("=" * 60)
