"""
sql_generator.py
----------------
Tool 2 of 6 — SQL Generator

Takes a natural language question + schema context and generates
a safe, executable SQL query using an LLM.

The LLM is ONLY used here — for SQL generation.
Everything else in the pipeline is deterministic.

Functions:
    generate_sql()   → convert natural language question to SQL
"""

import os
import json
import re
from typing import Optional
from openai import OpenAI
from dotenv import load_dotenv
from pathlib import Path
load_dotenv(Path(__file__).parent.parent / ".env")

# ------------------------------------------------------------------
# OpenAI client
# ------------------------------------------------------------------

client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
MODEL  = "gpt-4o-mini"


# ------------------------------------------------------------------
# System prompt — the core of SQL generation quality
# ------------------------------------------------------------------

SYSTEM_PROMPT = """You are an expert SQL generator for a Northwind enterprise database.

Your job is to convert natural language questions into safe, correct SQLite SQL queries.

## Database Schema
You will be given the relevant schema before each question.

## Rules you MUST follow
1. ONLY generate SELECT statements — never INSERT, UPDATE, DELETE, DROP, or ALTER
2. ALWAYS use table aliases for clarity (e.g. o for orders, c for customers)
3. ALWAYS limit results to 100 rows maximum using LIMIT unless the user asks for all
4. Use the views when relevant: order_revenue, product_sales_summary
5. For revenue calculations use: SUM(oi.unit_price * oi.quantity * (1 - oi.discount))
6. For date filtering use SQLite date functions: DATE(), strftime()
7. If the question is ambiguous, make the most reasonable assumption and note it
8. NEVER hallucinate column names — only use columns that exist in the schema provided

## Output format
Respond ONLY with valid JSON in this exact structure:
{
    "sql": "SELECT ...",
    "explanation": "Plain English explanation of what this query does",
    "assumptions": "Any assumptions made about ambiguous parts of the question",
    "tables_used": ["orders", "customers"],
    "confidence": "high|medium|low"
}

Nothing else. No markdown. No code blocks. Just the JSON object.

## Views available (use these when relevant)
- order_revenue: columns = order_id, customer_id, employee_id, order_date, status, revenue
- product_sales_summary: columns = product_id, product_name, category_name, total_units_sold, total_revenue

For revenue by category queries, JOIN order_items with products and categories directly.
Do NOT treat views as base tables with columns they don't have.
"""

# ------------------------------------------------------------------
# Tool 2: generate_sql()
# ------------------------------------------------------------------

def generate_sql(
    question: str,
    schema_context: dict,
    previous_error: Optional[str] = None,
    attempt: int = 1,
) -> dict:
    """
    Converts a natural language question into a SQL query.

    Args:
        question:       The user's natural language question.
        schema_context: Schema dict from search_schema() or get_schema().
        previous_error: If retrying after a failed query, pass the error message.
                        The LLM will use it to self-correct.
        attempt:        Current attempt number (max 3 retries).

    Returns:
        dict with:
        {
            "success": True,
            "sql": "SELECT ...",
            "explanation": "...",
            "assumptions": "...",
            "tables_used": [...],
            "confidence": "high|medium|low",
            "attempt": 1
        }
    """
    if attempt > 3:
        return {
            "success": False,
            "error":   "Max retry attempts reached. Could not generate valid SQL.",
            "attempt": attempt,
        }

    # ── Build schema string for prompt ─────────────────────────
    schema_str = _format_schema_for_prompt(schema_context)

    # ── Build user message ──────────────────────────────────────
    user_message = f"""## Relevant Schema
{schema_str}

## Question
{question}
"""

    # If retrying, add the error context
    if previous_error and attempt > 1:
        user_message += f"""
## Previous Attempt Failed
Your previous SQL query produced this error:
{previous_error}

Please fix the SQL and try again.
"""

    # ── Call the LLM ────────────────────────────────────────────
    try:
        response = client.chat.completions.create(
            model=MODEL,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user",   "content": user_message},
            ],
            temperature=0,        # Deterministic output for SQL generation
            max_tokens=1000,
        )

        raw_content = response.choices[0].message.content.strip()

        # ── Parse JSON response ─────────────────────────────────
        parsed = _parse_llm_response(raw_content)

        if not parsed["success"]:
            return {
                "success": False,
                "error":   f"Failed to parse LLM response: {parsed['error']}",
                "raw":     raw_content,
                "attempt": attempt,
            }

        return {
            "success":     True,
            "sql":         parsed["sql"],
            "explanation": parsed.get("explanation", ""),
            "assumptions": parsed.get("assumptions", ""),
            "tables_used": parsed.get("tables_used", []),
            "confidence":  parsed.get("confidence", "medium"),
            "attempt":     attempt,
        }

    except Exception as e:
        return {
            "success": False,
            "error":   str(e),
            "attempt": attempt,
        }


# ------------------------------------------------------------------
# Helper: format schema for prompt
# ------------------------------------------------------------------

def _format_schema_for_prompt(schema_context: dict) -> str:
    """
    Converts schema dict into a clean, readable string for the LLM prompt.
    Compact but complete — LLMs perform better with structured schema context.
    """
    lines = []

    # Handle both full schema response and search_schema response
    if "schemas" in schema_context:
        tables = schema_context["schemas"]
        views  = schema_context.get("relevant_views", [])
    elif "schema" in schema_context:
        tables = schema_context["schema"]["tables"]
        views  = schema_context["schema"].get("views", [])
    else:
        tables = schema_context
        views  = []

    for table_name, table_info in tables.items():
        lines.append(f"TABLE: {table_name}")
        for col in table_info["columns"]:
            pk_marker  = " [PK]" if col["primary_key"] else ""
            null_marker = " NOT NULL" if not col["nullable"] else ""
            lines.append(f"  - {col['name']} {col['type']}{pk_marker}{null_marker}")

        if table_info.get("foreign_keys"):
            for fk in table_info["foreign_keys"]:
                lines.append(
                    f"  FK: {fk['column']} → {fk['references_table']}.{fk['references_column']}"
                )
        lines.append(f"  Rows: {table_info['row_count']}")
        lines.append("")

    if views:
        lines.append(f"AVAILABLE VIEWS: {', '.join(views)}")

    return "\n".join(lines)


# ------------------------------------------------------------------
# Helper: parse LLM JSON response safely
# ------------------------------------------------------------------

def _parse_llm_response(raw: str) -> dict:
    """
    Safely parses the LLM's JSON response.
    Handles edge cases like accidental markdown fences.
    """
    # Strip markdown code fences if LLM added them accidentally
    cleaned = re.sub(r"```(?:json)?", "", raw).strip()
    cleaned = cleaned.strip("`").strip()

    try:
        data = json.loads(cleaned)

        # Validate required fields
        if "sql" not in data:
            return {"success": False, "error": "Response missing 'sql' field"}

        # Enforce SELECT only — safety net on top of the prompt instruction
        sql_upper = data["sql"].strip().upper()
        if not sql_upper.startswith("SELECT"):
            return {
                "success": False,
                "error":   f"Generated SQL is not a SELECT statement: {data['sql'][:50]}"
            }

        return {"success": True, **data}

    except json.JSONDecodeError as e:
        return {"success": False, "error": f"JSON parse error: {str(e)}"}


# ------------------------------------------------------------------
# Quick self-test (requires OPENAI_API_KEY)
# ------------------------------------------------------------------

if __name__ == "__main__":
    import sys
    sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
    from tools.schema_inspector import search_schema

    if not os.getenv("OPENAI_API_KEY"):
        print("❌ OPENAI_API_KEY not set. Export it first:")
        print("   export OPENAI_API_KEY=sk-...")
        exit(1)

    test_questions = [
        "Who are our top 5 customers by total revenue?",
        "How many orders were placed in 2024?",
        "What are the best selling products by category?",
    ]

    for question in test_questions:
        print(f"\n{'-' * 60}")
        print(f"QUESTION: {question}")
        print("-" * 60)

        # Get schema context first (as the agent would)
        schema_ctx = search_schema(question)

        # Generate SQL
        result = generate_sql(question, schema_ctx)

        if result["success"]:
            print(f"  SQL         : {result['sql']}")
            print(f"  Explanation : {result['explanation']}")
            print(f"  Assumptions : {result['assumptions']}")
            print(f"  Tables used : {result['tables_used']}")
            print(f"  Confidence  : {result['confidence']}")
            print(f"  Attempt     : {result['attempt']}")
        else:
            print(f"  ERROR: {result['error']}")