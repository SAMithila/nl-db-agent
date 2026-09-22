"""
validator.py
------------
Tool 3 of 6 — SQL Validator

Validates generated SQL BEFORE execution.
This is the safety gate between SQL generation and database execution.

The agent NEVER executes SQL that hasn't passed validation.

Validation runs against the database connected for the current session,
so user-uploaded databases are checked against their own schema — not the
demo database.

Functions:
    validate_sql()       → safety + syntax + complexity check
    complexity_score()   → score a query's complexity (1-10)
"""

import re


# ------------------------------------------------------------------
# Dangerous patterns — block these regardless of anything else
# ------------------------------------------------------------------

FORBIDDEN_PATTERNS = [
    r"\bDROP\b",
    r"\bDELETE\b",
    r"\bINSERT\b",
    r"\bUPDATE\b",
    r"\bALTER\b",
    r"\bCREATE\b",
    r"\bTRUNCATE\b",
    r"\bEXEC\b",
    r"\bEXECUTE\b",
    r"\bXP_\w+",          # SQL Server extended procs
    r"--",                # SQL comment (injection vector)
    r";.+",               # Multiple statements
    r"\bATTACH\b",        # SQLite ATTACH (access other db files)
    r"\bPRAGMA\b",        # SQLite PRAGMA (system commands)
]


# ------------------------------------------------------------------
# Tool 3A: validate_sql()
# ------------------------------------------------------------------

def validate_sql(sql: str, session_id: str = "default") -> dict:
    """
    Validates a SQL query before execution.
    Runs 3 checks in order: safety → syntax → complexity.

    Args:
        sql:        The SQL query string to validate.
        session_id: Database session whose schema the query is checked against.

    Returns:
        dict with:
        {
            "valid": True/False,
            "checks": {
                "safety":     {"passed": True, "details": "..."},
                "syntax":     {"passed": True, "details": "..."},
                "complexity": {"passed": True, "score": 3, "details": "..."}
            },
            "error": "reason if invalid"
        }
    """
    checks = {}

    # ── Check 1: Safety ────────────────────────────────────────
    # Must run first: EXPLAIN would accept DROP / DELETE as valid syntax.
    safety_result = _check_safety(sql)
    checks["safety"] = safety_result
    if not safety_result["passed"]:
        return {
            "valid":  False,
            "checks": checks,
            "error":  f"Safety check failed: {safety_result['details']}",
        }

    # ── Check 2: Syntax ────────────────────────────────────────
    syntax_result = _check_syntax(sql, session_id)
    checks["syntax"] = syntax_result
    if not syntax_result["passed"]:
        return {
            "valid":  False,
            "checks": checks,
            "error":  f"Syntax check failed: {syntax_result['details']}",
        }

    # ── Check 3: Complexity ────────────────────────────────────
    complexity_result = _check_complexity(sql)
    checks["complexity"] = complexity_result
    if not complexity_result["passed"]:
        return {
            "valid":  False,
            "checks": checks,
            "error":  f"Complexity check failed: {complexity_result['details']}",
        }

    return {
        "valid":  True,
        "checks": checks,
        "error":  None,
    }


# ------------------------------------------------------------------
# Tool 3B: complexity_score()
# ------------------------------------------------------------------

def complexity_score(sql: str) -> dict:
    """
    Scores a SQL query's complexity on a scale of 1-10.
    Used for observability logging and guardrail decisions.

    Score guide:
        1-3  → Simple  (single table, basic filter)
        4-6  → Medium  (joins, aggregations, date ranges)
        7-9  → Complex (subqueries, multiple joins, window functions)
        10   → Reject  (too complex for safe execution)

    Args:
        sql: The SQL query string.

    Returns:
        dict with score, tier, and breakdown.
    """
    score       = 1
    breakdown   = []
    sql_upper   = sql.upper()

    # Count JOINs
    join_count = len(re.findall(r"\bJOIN\b", sql_upper))
    if join_count >= 1:
        score += join_count
        breakdown.append(f"+{join_count} JOIN(s)")

    # Subqueries
    subquery_count = sql_upper.count("SELECT") - 1
    if subquery_count > 0:
        score += subquery_count * 2
        breakdown.append(f"+{subquery_count * 2} subquery")

    # Aggregations
    agg_functions = ["COUNT", "SUM", "AVG", "MAX", "MIN"]
    agg_count = sum(sql_upper.count(fn) for fn in agg_functions)
    if agg_count > 0:
        score += min(agg_count, 2)
        breakdown.append(f"+{min(agg_count, 2)} aggregation")

    # GROUP BY
    if "GROUP BY" in sql_upper:
        score += 1
        breakdown.append("+1 GROUP BY")

    # HAVING
    if "HAVING" in sql_upper:
        score += 1
        breakdown.append("+1 HAVING")

    # ORDER BY
    if "ORDER BY" in sql_upper:
        score += 1
        breakdown.append("+1 ORDER BY")

    # Window functions
    if "OVER(" in sql_upper or "OVER (" in sql_upper:
        score += 3
        breakdown.append("+3 window function")

    score = min(score, 10)

    if score <= 3:
        tier = "simple"
    elif score <= 6:
        tier = "medium"
    elif score <= 9:
        tier = "complex"
    else:
        tier = "reject"

    return {
        "score":     score,
        "tier":      tier,
        "breakdown": breakdown,
    }


# ------------------------------------------------------------------
# Internal check functions
# ------------------------------------------------------------------

def _check_safety(sql: str) -> dict:
    """Check for forbidden SQL patterns."""
    sql_upper = sql.upper().strip()

    # Must start with SELECT
    if not sql_upper.startswith("SELECT"):
        return {
            "passed":  False,
            "details": f"Query must start with SELECT. Got: {sql[:30]}",
        }

    # Check forbidden patterns
    for pattern in FORBIDDEN_PATTERNS:
        if re.search(pattern, sql_upper):
            return {
                "passed":  False,
                "details": f"Forbidden pattern detected: {pattern}",
            }

    return {"passed": True, "details": "All safety checks passed"}


def _check_syntax(sql: str, session_id: str = "default") -> dict:
    """
    Validate SQL against the session's connected database using EXPLAIN.
    The database parses and plans the query but does not execute it.
    Works for SQLite, PostgreSQL and MySQL.
    """
    try:
        from db_connector import get_active_engine
        engine = get_active_engine(session_id)
        with engine.connect() as conn:
            # exec_driver_sql sends SQL as-is, so ':' or '%' inside string
            # literals are not misread as bind parameters.
            conn.exec_driver_sql(f"EXPLAIN {sql}")
        return {"passed": True, "details": "Syntax is valid"}

    except Exception as e:
        # Unwrap SQLAlchemy's wrapper to surface the database's own message,
        # e.g. "no such column: t.ArtistId".
        return {"passed": False, "details": str(getattr(e, "orig", e))}


def _check_complexity(sql: str) -> dict:
    """Reject queries that are too complex to execute safely."""
    result = complexity_score(sql)

    if result["score"] >= 10:
        return {
            "passed":  False,
            "score":   result["score"],
            "details": "Query complexity too high (score 10). Simplify the query.",
        }

    return {
        "passed":  True,
        "score":   result["score"],
        "tier":    result["tier"],
        "details": f"Complexity score: {result['score']}/10 ({result['tier']})",
    }


# ------------------------------------------------------------------
# Quick self-test (runs against the default Chinook demo database)
# ------------------------------------------------------------------

if __name__ == "__main__":
    import os
    import sys
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

    test_cases = [
        # (description, sql, expected_valid)
        (
            "Valid simple query",
            "SELECT * FROM Customer LIMIT 10",
            True,
        ),
        (
            "Valid join query",
            """SELECT c.FirstName, SUM(il.UnitPrice * il.Quantity) AS revenue
               FROM Customer c
               JOIN Invoice i ON c.CustomerId = i.CustomerId
               JOIN InvoiceLine il ON i.InvoiceId = il.InvoiceId
               GROUP BY c.CustomerId
               ORDER BY revenue DESC LIMIT 5""",
            True,
        ),
        (
            "Invalid column (the Bug 17 join)",
            "SELECT a.Name FROM Artist a JOIN Track t ON a.ArtistId = t.ArtistId",
            False,
        ),
        (
            "DANGEROUS: DELETE statement",
            "DELETE FROM Invoice WHERE InvoiceId = 1",
            False,
        ),
        (
            "DANGEROUS: DROP table",
            "DROP TABLE Customer",
            False,
        ),
        (
            "DANGEROUS: SQL injection attempt",
            "SELECT * FROM Invoice; DROP TABLE Invoice--",
            False,
        ),
        (
            "Invalid syntax",
            "SELECT * FORM Customer",   # typo: FORM instead of FROM
            False,
        ),
    ]

    print("-" * 60)
    print("VALIDATOR TESTS")
    print("-" * 60)

    all_passed = True
    for desc, sql, expected in test_cases:
        result = validate_sql(sql)
        status = "✅" if result["valid"] == expected else "❌"
        if result["valid"] != expected:
            all_passed = False
        print(f"\n{status} {desc}")
        print(f"   Valid    : {result['valid']}")
        print(f"   Expected : {expected}")
        if not result["valid"]:
            print(f"   Error    : {result['error']}")
        else:
            score = result["checks"]["complexity"]
            print(f"   Complexity: {score['score']}/10 ({score['tier']})")

    print("\n" + "=" * 60)
    print(f"{'✅ All tests passed' if all_passed else '❌ Some tests failed'}")
    print("=" * 60)