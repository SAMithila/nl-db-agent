"""
metrics.py
----------
Phase 7C — Evaluation Framework (Updated for Agentic RAG)

Runs the full benchmark suite against the agent and measures
accuracy across all tiers: easy, medium, hard, rag, both, clarification.

Metrics tracked:
    SQL route:  execution success, row count accuracy, value accuracy
    RAG route:  source retrieval, keyword presence, LLM judge scores
    BOTH route: SQL accuracy + RAG faithfulness + synthesis quality
    All routes: latency, retry rate, hallucination rate

Usage:
    python -m evaluation.metrics              # full benchmark
    python -m evaluation.metrics --tier sql   # SQL only
    python -m evaluation.metrics --tier rag   # RAG only
    python -m evaluation.metrics --judge      # include LLM judge
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import json
import time
import argparse
from pathlib import Path
from datetime import datetime
from dotenv import load_dotenv
load_dotenv(Path(__file__).parent.parent / ".env")

from agent.graph          import run_query
from agent.state          import AgentState
from observability.tracer import save_trace


BENCHMARK_PATH = Path(__file__).parent / "evaluation_dataset.json"
RESULTS_PATH   = Path(__file__).parent / "eval_results.json"

# Tiers that use SQL pipeline
SQL_TIERS  = {"easy", "medium", "hard"}

# Tiers that use RAG
RAG_TIERS  = {"rag"}

# Tiers that use both
BOTH_TIERS = {"both"}


# ------------------------------------------------------------------
# Core evaluation runner
# ------------------------------------------------------------------

def run_evaluation(
    user_id:    str  = "alice",
    verbose:    bool = True,
    tier_filter: str = None,
    use_judge:  bool = False,
) -> dict:
    """
    Runs all benchmark queries and computes accuracy metrics.

    Args:
        user_id:     User to run queries as.
        verbose:     Print results as they run.
        tier_filter: Only run queries of this tier (sql/rag/both/all).
        use_judge:   Run LLM-as-judge on each answer.

    Returns:
        Full evaluation report dict.
    """
    with open(BENCHMARK_PATH) as f:
        benchmarks = json.load(f)

    # Filter by tier if specified
    if tier_filter and tier_filter != "all":
        if tier_filter == "sql":
            benchmarks = [b for b in benchmarks if b.get("tier") in SQL_TIERS]
        elif tier_filter == "rag":
            benchmarks = [b for b in benchmarks if b.get("tier") in RAG_TIERS]
        elif tier_filter == "both":
            benchmarks = [b for b in benchmarks if b.get("tier") in BOTH_TIERS]
        else:
            benchmarks = [b for b in benchmarks if b.get("tier") == tier_filter]

    results    = []
    start_time = time.time()

    if verbose:
        print("=" * 70)
        print("AGENTIC RAG EVALUATION RUN")
        print(f"Timestamp   : {datetime.now().isoformat()}")
        print(f"User        : {user_id}")
        print(f"Queries     : {len(benchmarks)}")
        print(f"LLM Judge   : {'enabled' if use_judge else 'disabled'}")
        print("=" * 70)

    for bench in benchmarks:
        result = _evaluate_single(bench, user_id, verbose, use_judge)
        results.append(result)

        if result.get("state"):
            save_trace(result["state"])
            result.pop("state")

    total_time = round(time.time() - start_time, 2)

    report = _compute_metrics(results, total_time)
    report["results"] = results

    with open(RESULTS_PATH, "w") as f:
        json.dump(report, f, indent=2, default=str)

    if verbose:
        _print_report(report)

    return report


# ------------------------------------------------------------------
# Single query evaluator
# ------------------------------------------------------------------

def _evaluate_single(
    bench:     dict,
    user_id:   str,
    verbose:   bool,
    use_judge: bool,
) -> dict:
    """Runs one benchmark query and evaluates the result."""

    query_id = bench["id"]
    tier     = bench["tier"]
    question = bench["question"]
    route    = bench.get("route", "SQL")

    if verbose:
        route_icon = {"SQL": "🗄️ ", "RAG": "📄", "BOTH": "🔀"}.get(route, "  ")
        print(f"\n[{query_id}] {tier.upper()} {route_icon} {question}")

    start = time.time()
    state = run_query(question, user_id)
    elapsed_ms = round((time.time() - start) * 1000)

    result = {
        "id":          query_id,
        "tier":        tier,
        "route":       route,
        "question":    question,
        "elapsed_ms":  elapsed_ms,
        "retry_count": state.retry_count,
        "state":       state,
    }

    # ── Clarification tier ──────────────────────────────────────
    if tier == "clarification":
        triggered = state.needs_clarification
        result.update({
            "clarification_triggered": triggered,
            "passed":                  triggered,
            "failure_reason":          None if triggered else "Clarification not triggered",
        })
        status = "✅" if triggered else "❌"
        if verbose:
            print(f"  {status} Clarification triggered: {triggered} ({elapsed_ms}ms)")
        return result

    # ── RAG tier ────────────────────────────────────────────────
    if tier in RAG_TIERS:
        return _evaluate_rag(bench, state, result, verbose, use_judge, elapsed_ms)

    # ── BOTH tier ───────────────────────────────────────────────
    if tier in BOTH_TIERS:
        return _evaluate_both(bench, state, result, verbose, use_judge, elapsed_ms)

    # ── SQL tiers (easy, medium, hard) ──────────────────────────
    return _evaluate_sql(bench, state, result, verbose, use_judge, elapsed_ms)


def _evaluate_sql(bench, state, result, verbose, use_judge, elapsed_ms):
    """Evaluates SQL route answers."""
    if not state.execution_success:
        result.update({
            "passed":         False,
            "failure_reason": state.error or "Execution failed",
            "row_count":      0,
            "sql":            state.generated_sql,
            "answer":         None,
        })
        if verbose:
            print(f"  ❌ FAILED: {state.error} ({elapsed_ms}ms)")
        return result

    exec_result = state.execution_result
    row_count   = exec_result["row_count"]
    columns     = exec_result["columns"]
    answer      = state.final_response.get("summary", "") if state.final_response else ""

    # Row count check
    expected_rows = bench.get("expected_row_count")
    row_count_ok  = True
    if expected_rows is not None:
        row_count_ok = (row_count == expected_rows)

    # Column check
    expected_cols = bench.get("expected_columns")
    columns_ok = True
    if expected_cols:
        columns_lower = [c.lower() for c in columns]
        columns_ok = all(ec.lower() in columns_lower for ec in expected_cols)

    # Value check — flexible: check if expected value appears anywhere in first row
    expected_val = bench.get("expected_value")
    value_ok = True
    if expected_val and exec_result["rows"]:
        first_row_values = [str(v) for v in exec_result["rows"][0]]
        for key, val in expected_val.items():
            if str(val) not in first_row_values:
                value_ok = False
                break

    passed = row_count_ok and columns_ok and value_ok
    failure_reasons = []
    if not row_count_ok:
        failure_reasons.append(f"row_count: expected {expected_rows}, got {row_count}")
    if not columns_ok:
        failure_reasons.append(f"missing columns: {expected_cols}")
    if not value_ok:
        failure_reasons.append(f"value mismatch")

    result.update({
        "passed":         passed,
        "failure_reason": "; ".join(failure_reasons) if failure_reasons else None,
        "row_count":      row_count,
        "columns":        columns,
        "sql":            state.generated_sql,
        "sql_confidence": state.sql_confidence,
        "answer":         answer,
    })

    # LLM judge
    if use_judge and answer:
        from evaluation.llm_judge import judge_answer
        result["judge_scores"] = judge_answer(
            question       = bench["question"],
            answer         = answer,
            route          = "SQL",
            sql            = state.generated_sql,
            row_count      = row_count,
            expected_value = bench.get("expected_value"),
        )

    status = "✅" if passed else "❌"
    if verbose:
        msg = f"rows={row_count}"
        if not passed:
            msg += f" | FAIL: {'; '.join(failure_reasons)}"
        print(f"  {status} {msg} ({elapsed_ms}ms)")

    return result


def _evaluate_rag(bench, state, result, verbose, use_judge, elapsed_ms):
    """Evaluates RAG route answers."""
    final    = state.final_response or {}
    answer   = final.get("summary", "")
    success  = final.get("success", False)

    # Check if RAG retrieved anything
    rag_success = state.rag_success
    rag_sources = state.rag_sources or []
    rag_context = state.rag_context or ""

    # Check expected sources
    expected_sources = bench.get("expected_sources", [])
    sources_retrieved = [s.get("source", "") for s in rag_sources]
    source_hit = True
    if expected_sources:
        source_hit = any(
            any(es in sr for sr in sources_retrieved)
            for es in expected_sources
        )

    # Check expected keywords in answer
    expected_keywords = bench.get("expected_content_keywords", [])
    keyword_hits = []
    if expected_keywords and answer:
        answer_lower = answer.lower()
        for kw in expected_keywords:
            if kw.lower() in answer_lower:
                keyword_hits.append(kw)
    keyword_hit_rate = len(keyword_hits) / len(expected_keywords) if expected_keywords else 1.0

    passed = success and rag_success and keyword_hit_rate >= 0.5

    result.update({
        "passed":            passed,
        "failure_reason":    None if passed else f"keyword_hit_rate={keyword_hit_rate:.0%}, source_hit={source_hit}",
        "rag_success":       rag_success,
        "sources_retrieved": sources_retrieved,
        "source_hit":        source_hit,
        "keyword_hit_rate":  round(keyword_hit_rate, 2),
        "keywords_found":    keyword_hits,
        "answer":            answer,
        "rag_context":       rag_context[:2000],
        "rag_sources":       rag_sources,
    })

    # LLM judge
    if use_judge and answer:
        from evaluation.llm_judge import judge_answer
        result["judge_scores"] = judge_answer(
            question          = bench["question"],
            answer            = answer,
            route             = "RAG",
            rag_context       = state.rag_context, 
            rag_sources       = rag_sources,
            expected_keywords = expected_keywords,
        )
        # Override passed with judge score if available
        if result["judge_scores"] and result["judge_scores"].get("overall", 0) < 3.5:
            result["passed"] = False
            result["failure_reason"] = f"LLM judge score too low: {result['judge_scores']['overall']}/5"

    status = "✅" if passed else "❌"
    if verbose:
        kw_str = f"keywords={len(keyword_hits)}/{len(expected_keywords)}" if expected_keywords else "no keywords expected"
        print(f"  {status} {kw_str}, sources={len(rag_sources)} ({elapsed_ms}ms)")

    return result


def _evaluate_both(bench, state, result, verbose, use_judge, elapsed_ms):
    """Evaluates BOTH route answers (SQL + RAG combined)."""
    final   = state.final_response or {}
    answer  = final.get("summary", "")
    success = final.get("success", False)

    sql_ok  = state.execution_success
    rag_ok  = state.rag_success

    passed = success and (sql_ok or rag_ok)

    result.update({
        "passed":         passed,
        "failure_reason": None if passed else f"sql_ok={sql_ok}, rag_ok={rag_ok}",
        "sql_success":    sql_ok,
        "rag_success":    rag_ok,
        "row_count":      state.execution_result["row_count"] if sql_ok and state.execution_result else 0,
        "sources_retrieved": [s.get("source", "") for s in (state.rag_sources or [])],
        "answer":         answer,
        "sql":            state.generated_sql,
    })

    # LLM judge (most important for BOTH route)
    if use_judge and answer:
        from evaluation.llm_judge import judge_answer
        result["judge_scores"] = judge_answer(
            question    = bench["question"],
            answer      = answer,
            route       = "BOTH",
            sql         = state.generated_sql,
            row_count   = result["row_count"],
            rag_context = state.rag_context,
            rag_sources = state.rag_sources,
        )
        if result["judge_scores"] and result["judge_scores"].get("overall", 0) < 3.5:
            result["passed"] = False
            result["failure_reason"] = f"LLM judge score: {result['judge_scores']['overall']}/5"

    status = "✅" if passed else "❌"
    if verbose:
        print(f"  {status} sql={sql_ok}, rag={rag_ok} ({elapsed_ms}ms)")

    return result


# ------------------------------------------------------------------
# Metrics computation
# ------------------------------------------------------------------

def _compute_metrics(results: list, total_time: float) -> dict:
    """Computes aggregate metrics across all results."""

    by_tier = {
        "easy": [], "medium": [], "hard": [],
        "rag": [], "both": [], "clarification": []
    }
    for r in results:
        tier = r.get("tier", "unknown")
        if tier in by_tier:
            by_tier[tier].append(r)

    def tier_stats(tier_results):
        if not tier_results:
            return {"count": 0, "passed": 0, "accuracy": 0}
        passed = sum(1 for r in tier_results if r.get("passed"))
        return {
            "count":    len(tier_results),
            "passed":   passed,
            "accuracy": round(passed / len(tier_results) * 100, 1),
        }

    total   = len(results)
    passed  = sum(1 for r in results if r.get("passed"))
    retried = sum(1 for r in results if r.get("retry_count", 0) > 0)

    latencies = [r["elapsed_ms"] for r in results if "elapsed_ms" in r]
    avg_ms    = round(sum(latencies) / len(latencies)) if latencies else 0

    # Hallucination rate from LLM judge
    judge_results = [r for r in results if r.get("judge_scores")]
    hallucinations = sum(1 for r in judge_results if r["judge_scores"].get("hallucination"))
    hallucination_rate = round(hallucinations / len(judge_results) * 100, 1) if judge_results else None

    # Average judge scores by route
    judge_by_route = {}
    for route in ["SQL", "RAG", "BOTH"]:
        route_judged = [r for r in judge_results if r.get("route") == route]
        if route_judged:
            avg_score = round(
                sum(r["judge_scores"].get("overall", 0) for r in route_judged) / len(route_judged), 2
            )
            judge_by_route[route] = {"count": len(route_judged), "avg_score": avg_score}

    return {
        "timestamp": datetime.now().isoformat(),
        "summary": {
            "total_queries":     total,
            "passed":            passed,
            "failed":            total - passed,
            "overall_accuracy":  round(passed / total * 100, 1) if total else 0,
            "retry_rate":        round(retried / total * 100, 1) if total else 0,
            "avg_latency_ms":    avg_ms,
            "total_time_s":      total_time,
            "hallucination_rate": hallucination_rate,
        },
        "by_tier": {
            "easy":          tier_stats(by_tier["easy"]),
            "medium":        tier_stats(by_tier["medium"]),
            "hard":          tier_stats(by_tier["hard"]),
            "rag":           tier_stats(by_tier["rag"]),
            "both":          tier_stats(by_tier["both"]),
            "clarification": tier_stats(by_tier["clarification"]),
        },
        "llm_judge": {
            "enabled":           bool(judge_results),
            "queries_judged":    len(judge_results),
            "hallucination_rate": hallucination_rate,
            "by_route":          judge_by_route,
        },
    }


# ------------------------------------------------------------------
# Report printer
# ------------------------------------------------------------------

def _print_report(report: dict) -> None:
    s  = report["summary"]
    bt = report["by_tier"]
    lj = report.get("llm_judge", {})

    print(f"\n{'=' * 70}")
    print("AGENTIC RAG EVALUATION RESULTS")
    print(f"{'=' * 70}")
    print(f"  Overall accuracy   : {s['overall_accuracy']}%  ({s['passed']}/{s['total_queries']})")
    print(f"  Retry rate         : {s['retry_rate']}%")
    print(f"  Avg latency        : {s['avg_latency_ms']}ms")
    print(f"  Total time         : {s['total_time_s']}s")
    if s.get("hallucination_rate") is not None:
        print(f"  Hallucination rate : {s['hallucination_rate']}%")

    print(f"\n── By Tier ──")
    tier_icons = {"easy": "⚡", "medium": "🔧", "hard": "💪", "rag": "📄", "both": "🔀", "clarification": "❓"}
    for tier, stats in bt.items():
        if stats["count"] > 0:
            bar  = "█" * int(stats["accuracy"] / 10)
            icon = tier_icons.get(tier, "  ")
            print(f"  {icon} {tier:15s} : {stats['accuracy']:5.1f}%  {bar}  ({stats['passed']}/{stats['count']})")

    if lj.get("enabled") and lj.get("by_route"):
        print(f"\n── LLM Judge Scores (avg/5) ──")
        for route, data in lj["by_route"].items():
            bar = "█" * int(data["avg_score"])
            print(f"  {route:6s} : {data['avg_score']:.1f}/5  {bar}  ({data['count']} queries)")

    failures = [r for r in report.get("results", []) if not r.get("passed")]
    if failures:
        print(f"\n── Failed Queries ──")
        for r in failures:
            icon = {"SQL": "🗄️ ", "RAG": "📄", "BOTH": "🔀"}.get(r.get("route"), "  ")
            print(f"  {icon} [{r['id']}] {r['question']}")
            print(f"         Reason: {r.get('failure_reason', 'unknown')}")

    print(f"{'=' * 70}")
    print(f"Results saved to: evaluation/eval_results.json")
    print(f"{'=' * 70}")


# ------------------------------------------------------------------
# Load previous results
# ------------------------------------------------------------------

def load_results() -> dict:
    """Loads the most recent evaluation results."""
    if not RESULTS_PATH.exists():
        return {"error": "No evaluation results found. Run evaluation first."}
    with open(RESULTS_PATH) as f:
        return json.load(f)


def get_metrics_summary() -> dict:
    """Returns a summary dict for the /metrics API endpoint."""
    results = load_results()
    if "error" in results:
        return results
    return {
        "summary":   results.get("summary", {}),
        "by_tier":   results.get("by_tier", {}),
        "llm_judge": results.get("llm_judge", {}),
        "timestamp": results.get("timestamp", ""),
    }


# ------------------------------------------------------------------
# CLI entry point
# ------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run Agentic RAG evaluation benchmark")
    parser.add_argument("--tier",  type=str, default="all", help="Filter tier: sql/rag/both/easy/medium/hard/all")
    parser.add_argument("--judge", action="store_true",      help="Enable LLM-as-judge scoring")
    parser.add_argument("--user",  type=str, default="alice", help="User ID to run as")
    args = parser.parse_args()

    run_evaluation(
        user_id     = args.user,
        verbose     = True,
        tier_filter = args.tier,
        use_judge   = args.judge,
    )
