"""
graph.py
--------
LangGraph Agent Graph — Phase 7: Agentic RAG

Wires SQL pipeline + RAG retrieval into a unified agentic system.
The router decides which path(s) to take for each question.

Flow:
    START
      → clarify_node       (detect ambiguity)
      → router_node        (LLM decides: SQL | RAG | BOTH)
      ↓
    SQL path:              RAG path:
      → schema_node          → rag_node
      → generate_node            ↓
      → validate_node        → format_node
      → guardrails_node
      → execute_node
      → format_node
      ↓
    BOTH path:
      → schema_node + rag_node (parallel)
      → generate_node → validate → guardrails → execute
      → format_node (combines SQL result + RAG context)

    END
"""

import sys
import os
import asyncio
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from typing import Literal
from langgraph.graph import StateGraph, START, END
from agent.state import AgentState

# SQL tools
from tools.schema_inspector import search_schema
from tools.sql_generator    import generate_sql
from tools.validator        import validate_sql
from tools.executor         import execute_query
from tools.clarifier        import needs_clarification, generate_clarification
from tools.formatter        import format_response, format_error

# Guardrails
from guardrails.permissions import check_permission
from guardrails.limits      import check_limits, record_query
from guardrails.safety      import safety_check

# Phase 7: RAG
from agent.router           import route_question, Route
from rag.retriever          import retrieve_with_context


# ------------------------------------------------------------------
# Node 1: Clarify
# ------------------------------------------------------------------

def clarify_node(state: AgentState) -> AgentState:
    """Checks if the question is ambiguous."""
    state.add_trace("clarify", f"Checking question: '{state.question}'")

    check = needs_clarification(state.question)

    if check["needs_clarification"] and check["confidence"] == "high":
        clarification = generate_clarification(
            state.question,
            check["ambiguities"]
        )
        state.needs_clarification    = True
        state.clarification_response = clarification
        state.add_trace("clarify", f"Ambiguity detected: {[a['type'] for a in check['ambiguities']]}")
    else:
        state.needs_clarification = False
        state.add_trace("clarify", "Question is clear — proceeding")

    return state


# ------------------------------------------------------------------
# Node 2: Router (NEW — Phase 7)
# ------------------------------------------------------------------

def router_node(state: AgentState) -> AgentState:
    """
    LLM-based router — decides SQL, RAG, or BOTH.
    Sets state.route which controls downstream flow.
    """
    state.add_trace("router", f"Routing question: '{state.question}'")

    result = route_question(state.question)

    state.route        = result["route"].value   # "SQL" | "RAG" | "BOTH"
    state.route_reason = result["reason"]
    state.sql_focus    = result["sql_focus"]
    state.rag_focus    = result["rag_focus"]

    state.add_trace("router", f"Route: {state.route} — {state.route_reason}")
    return state


# ------------------------------------------------------------------
# Node 3: RAG (NEW — Phase 7)
# ------------------------------------------------------------------

def rag_node(state: AgentState) -> AgentState:
    """
    Retrieves relevant document chunks from Pinecone.
    Uses rag_focus (from router) as the search query if available,
    otherwise falls back to the original question.
    """
    search_query = state.rag_focus or state.question
    state.add_trace("rag", f"Searching documents: '{search_query}'")

    try:
        result = retrieve_with_context(search_query, top_k=5)

        state.rag_context = result["context"]
        state.rag_chunks  = result["chunks"]
        state.rag_sources = result["sources"]
        state.rag_success = len(result["chunks"]) > 0

        source_titles = [s["title"] for s in result["sources"]]
        state.add_trace(
            "rag",
            f"Retrieved {len(result['chunks'])} chunks from: {source_titles}"
        )

    except Exception as e:
        state.rag_success = False
        state.rag_context = ""
        state.add_trace("rag", f"ERROR: {e}")

    return state


# ------------------------------------------------------------------
# Node 4: Schema
# ------------------------------------------------------------------

def schema_node(state: AgentState) -> AgentState:
    """Retrieves relevant schema context for SQL generation."""
    state.add_trace("schema", "Retrieving relevant schema")

    result = search_schema(state.question)

    if result["success"]:
        state.schema_context = result
        state.add_trace("schema", f"Found relevant tables: {result['relevant_tables']}")
    else:
        state.error       = f"Schema retrieval failed: {result.get('error')}"
        state.error_stage = "schema"
        state.add_trace("schema", f"ERROR: {state.error}")

    return state


# ------------------------------------------------------------------
# Node 5: Generate
# ------------------------------------------------------------------

def generate_node(state: AgentState) -> AgentState:
    """Calls GPT-4o to convert the question into SQL."""
    state.generation_attempt += 1
    state.add_trace("generate", f"Generating SQL (attempt {state.generation_attempt})")

    previous_error = None
    if state.retry_count > 0 and state.validation_result:
        previous_error = state.validation_result.get("error")

    result = generate_sql(
        question       = state.question,
        schema_context = state.schema_context,
        previous_error = previous_error,
        attempt        = state.generation_attempt,
    )

    if result["success"]:
        state.generated_sql   = result["sql"]
        state.sql_explanation = result["explanation"]
        state.sql_assumptions = result["assumptions"]
        state.sql_confidence  = result["confidence"]
        state.add_trace("generate", f"SQL generated (confidence: {result['confidence']})")
    else:
        state.error       = f"SQL generation failed: {result.get('error')}"
        state.error_stage = "generation"
        state.add_trace("generate", f"ERROR: {state.error}")

    return state


# ------------------------------------------------------------------
# Node 6: Validate
# ------------------------------------------------------------------

def validate_node(state: AgentState) -> AgentState:
    """Validates the generated SQL."""
    state.add_trace("validate", "Validating SQL")

    result = validate_sql(state.generated_sql)
    state.validation_result = result

    if result["valid"]:
        state.validation_passed = True
        complexity = result["checks"]["complexity"]
        state.add_trace("validate", f"Valid — complexity: {complexity['score']}/10 ({complexity['tier']})")
    else:
        state.validation_passed = False
        state.retry_count      += 1
        state.add_trace("validate", f"FAILED: {result['error']} (retry {state.retry_count}/{state.max_retries})")

    return state


# ------------------------------------------------------------------
# Node 7: Guardrails
# ------------------------------------------------------------------

def guardrails_node(state: AgentState) -> AgentState:
    """Runs permission, limits, and safety checks."""
    state.add_trace("guardrails", "Running guardrail checks")

    perm = check_permission(state.generated_sql, state.user_id)
    state.permission_result = perm
    if not perm["allowed"]:
        state.guardrails_passed = False
        state.error             = perm["reason"]
        state.error_stage       = "permission"
        state.add_trace("guardrails", f"PERMISSION DENIED: {perm['reason']}")
        return state

    limits = check_limits(state.generated_sql, state.user_id)
    state.limits_result = limits
    if not limits["allowed"]:
        state.guardrails_passed = False
        state.error             = limits["reason"]
        state.error_stage       = "limits"
        state.add_trace("guardrails", f"LIMITS EXCEEDED: {limits['reason']}")
        return state

    safety = safety_check(state.generated_sql, state.user_id)
    state.safety_result = safety
    if not safety["safe"]:
        state.guardrails_passed = False
        state.error             = safety["reason"]
        state.error_stage       = "safety"
        state.add_trace("guardrails", f"SAFETY BLOCKED: {safety['reason']}")
        return state

    state.guardrails_passed = True
    state.add_trace("guardrails", "All guardrails passed ✓")
    return state


# ------------------------------------------------------------------
# Node 8: Execute
# ------------------------------------------------------------------

def execute_node(state: AgentState) -> AgentState:
    """Executes validated SQL against the database."""
    state.add_trace("execute", "Executing query")

    result = execute_query(state.generated_sql)
    state.execution_result = result

    if result["success"]:
        state.execution_success = True
        record_query(state.user_id, result["execution_ms"])
        state.add_trace("execute", f"Success — {result['row_count']} rows in {result['execution_ms']}ms")
    else:
        state.execution_success = False
        state.error             = result["error"]
        state.error_stage       = "execution"
        state.add_trace("execute", f"ERROR: {result['error']}")

    return state


# ------------------------------------------------------------------
# Node 9: Format (updated for Phase 7 — handles SQL, RAG, BOTH)
# ------------------------------------------------------------------

def format_node(state: AgentState) -> AgentState:
    """
    Formats the final response.
    Handles three cases:
        - SQL only: standard SQL result formatting
        - RAG only: document-grounded answer
        - BOTH: combined SQL data + document context
    """
    state.add_trace("format", f"Formatting response (route={state.route})")

    # Error case
    if state.has_error() and not state.rag_success:
        state.final_response = format_error(
            question = state.question,
            error    = state.error or "Unknown error",
            stage    = state.error_stage or "unknown",
        )
        state.add_trace("format", f"Error response for stage: {state.error_stage}")
        return state

    # RAG-only case
    if state.route == "RAG":
        if state.rag_success:
            state.final_response = _format_rag_response(state)
        else:
            state.final_response = format_error(
                question = state.question,
                error    = "No relevant documents found for this question.",
                stage    = "rag",
            )
        return state

    # SQL-only or BOTH case
    if state.execution_success:
        exec_result = state.execution_result

        # For BOTH: inject RAG context into the formatter
        rag_context = state.rag_context if state.route == "BOTH" and state.rag_success else None

        state.final_response = format_response(
            question     = state.question,
            sql          = state.generated_sql,
            columns      = exec_result["columns"],
            rows         = exec_result["rows"],
            execution_ms = exec_result["execution_ms"],
            truncated    = exec_result["truncated"],
            explanation  = state.sql_explanation,
            assumptions  = state.sql_assumptions,
            rag_context  = rag_context,
            rag_sources  = state.rag_sources if rag_context else [],
        )
        state.add_trace("format", "Response formatted successfully")
    else:
        state.final_response = format_error(
            question = state.question,
            error    = state.error or "Query execution failed",
            stage    = state.error_stage or "execution",
        )

    return state


def _format_rag_response(state: AgentState) -> dict:
    """
    Formats a RAG-only response using GPT-4o to synthesize
    the retrieved document chunks into a coherent answer.
    """
    import os
    from openai import OpenAI

    client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

    system_prompt = """You are a helpful music industry analyst. 
Answer the user's question based ONLY on the provided document context.
Be specific and cite which document you're drawing from.
If the context doesn't contain enough information, say so clearly."""

    user_prompt = f"""Question: {state.question}

Document Context:
{state.rag_context}

Provide a clear, specific answer based on the documents above.
Cite the source document(s) in your answer."""

    try:
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user",   "content": user_prompt},
            ],
            temperature=0.1,
            max_tokens=500,
        )

        answer = response.choices[0].message.content

        # Build key insights from source list
        insights = [
            f"Source: {s['title']} ({s['publisher']}, {s['year']})"
            for s in state.rag_sources
        ]

        return {
            "success":      True,        # ADD THIS
            "summary":      answer,
            "key_insights": insights,
            "route":        "RAG",
            "sources":      state.rag_sources,
        }

    except Exception as e:
        return {
            "success":      True,        # ADD THIS
            "summary":      f"Retrieved {len(state.rag_chunks)} relevant passages but could not synthesize: {e}",
            "key_insights": [c["text"][:200] for c in state.rag_chunks[:3]],
            "route":        "RAG",
            "sources":      state.rag_sources,
        }


# ------------------------------------------------------------------
# Routing functions
# ------------------------------------------------------------------

def route_after_clarify(state: AgentState) -> Literal["router", "end"]:
    if state.needs_clarification:
        return "end"
    return "router"


def route_after_router(state: AgentState) -> Literal["schema", "rag", "schema_and_rag"]:
    """
    After routing decision:
    - SQL  → go to schema (SQL pipeline)
    - RAG  → go to rag node
    - BOTH → go to schema (RAG runs in parallel inside schema_and_rag)
    """
    if state.route == "RAG":
        return "rag"
    elif state.route == "BOTH":
        return "schema_and_rag"
    else:
        return "schema"


def route_after_schema(state: AgentState) -> Literal["generate", "format"]:
    if state.has_error():
        return "format"
    return "generate"


def route_after_generate(state: AgentState) -> Literal["validate", "format"]:
    if state.has_error():
        return "format"
    return "validate"


def route_after_validate(state: AgentState) -> Literal["guardrails", "generate", "format"]:
    if state.validation_passed:
        return "guardrails"
    if state.can_retry():
        return "generate"
    return "format"


def route_after_guardrails(state: AgentState) -> Literal["execute", "format"]:
    if state.guardrails_passed:
        return "execute"
    return "format"


def route_after_rag(state: AgentState) -> Literal["format"]:
    """RAG-only path always goes to format."""
    return "format"


def route_after_execute(state: AgentState) -> Literal["format"]:
    return "format"


# ------------------------------------------------------------------
# BOTH path: schema + RAG run sequentially then merge
# ------------------------------------------------------------------

def schema_and_rag_node(state: AgentState) -> AgentState:
    """
    For BOTH route: runs schema retrieval and RAG retrieval,
    then continues to SQL generation with RAG context available.
    """
    # Run schema
    state = schema_node(state)

    # Run RAG in parallel (using asyncio if available, else sequential)
    state = rag_node(state)

    return state


def route_after_schema_and_rag(state: AgentState) -> Literal["generate", "format"]:
    if state.has_error():
        return "format"
    return "generate"


# ------------------------------------------------------------------
# Build the graph
# ------------------------------------------------------------------

def build_graph() -> StateGraph:
    """
    Constructs and compiles the Agentic RAG LangGraph.

    Phase 7 additions:
        - router_node: LLM decides SQL/RAG/BOTH
        - rag_node: Pinecone document retrieval
        - schema_and_rag_node: combined node for BOTH path
        - Updated format_node: handles all three response types
    """
    graph = StateGraph(AgentState)

    # Add all nodes
    graph.add_node("clarify",         clarify_node)
    graph.add_node("router",          router_node)          # NEW
    graph.add_node("rag",             rag_node)             # NEW
    graph.add_node("schema_and_rag",  schema_and_rag_node)  # NEW
    graph.add_node("schema",          schema_node)
    graph.add_node("generate",        generate_node)
    graph.add_node("validate",        validate_node)
    graph.add_node("guardrails",      guardrails_node)
    graph.add_node("execute",         execute_node)
    graph.add_node("format",          format_node)

    # Entry
    graph.add_edge(START, "clarify")

    # Clarify → Router or END
    graph.add_conditional_edges(
        "clarify",
        route_after_clarify,
        {"router": "router", "end": END}
    )

    # Router → SQL path | RAG path | BOTH path
    graph.add_conditional_edges(
        "router",
        route_after_router,
        {
            "schema":         "schema",
            "rag":            "rag",
            "schema_and_rag": "schema_and_rag",
        }
    )

    # RAG-only path → format
    graph.add_conditional_edges(
        "rag",
        route_after_rag,
        {"format": "format"}
    )

    # BOTH path → generate (after schema+rag)
    graph.add_conditional_edges(
        "schema_and_rag",
        route_after_schema_and_rag,
        {"generate": "generate", "format": "format"}
    )

    # SQL pipeline (unchanged from Phase 1-6)
    graph.add_conditional_edges("schema",     route_after_schema,     {"generate": "generate",     "format": "format"})
    graph.add_conditional_edges("generate",   route_after_generate,   {"validate": "validate",     "format": "format"})
    graph.add_conditional_edges("validate",   route_after_validate,   {"guardrails": "guardrails", "generate": "generate", "format": "format"})
    graph.add_conditional_edges("guardrails", route_after_guardrails, {"execute": "execute",       "format": "format"})
    graph.add_conditional_edges("execute",    route_after_execute,    {"format": "format"})

    graph.add_edge("format", END)

    return graph.compile()


# ------------------------------------------------------------------
# Run a single question
# ------------------------------------------------------------------

def run_query(question: str, user_id: str = "default_user", session_id: str = "default") -> AgentState:
    """
    Run a natural language question through the full Agentic RAG pipeline.
    Automatically routes to SQL, RAG, or both based on question type.
    """
    graph         = build_graph()
    initial_state = AgentState(question=question, user_id=user_id, session_id=session_id)
    final_state   = graph.invoke(initial_state)

    if isinstance(final_state, dict):
        state = AgentState(**final_state)
    else:
        state = final_state

    return state
