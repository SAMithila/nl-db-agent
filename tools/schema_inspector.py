"""
schema_inspector.py
-------------------
Tool 1 of 6 — Schema Inspector

Gives the agent a complete understanding of the database structure
BEFORE it attempts to generate any SQL.

Now supports PostgreSQL, MySQL, and SQLite via SQLAlchemy inspect().

Functions:
    get_schema()         → full schema of all tables
    search_schema()      → find relevant tables for a question
    get_table_sample()   → show example rows from a table
"""

import os
import sys
from typing import Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))


def _get_engine(session_id: str = "default"):
    """Get SQLAlchemy engine via dynamic connector."""
    from db_connector import get_active_engine
    return get_active_engine(session_id)


# ------------------------------------------------------------------
# Tool 1A: get_schema()
# ------------------------------------------------------------------

def get_schema(
    table_name: Optional[str] = None,
    session_id: str           = "default",
) -> dict:
    """
    Returns the full database schema or a single table's schema.
    Works with PostgreSQL, MySQL, and SQLite.
    """
    try:
        from sqlalchemy import inspect as sa_inspect, text

        engine    = _get_engine(session_id)
        inspector = sa_inspect(engine)

        # Get all table names
        all_tables = inspector.get_table_names()
        if table_name:
            all_tables = [t for t in all_tables if t == table_name]

        # Get views (SQLite + PostgreSQL + MySQL)
        try:
            views = inspector.get_view_names()
        except Exception:
            views = []

        schema = {"tables": {}, "views": views}

        with engine.connect() as conn:
            for tbl in all_tables:
                # Columns
                columns = []
                for col in inspector.get_columns(tbl):
                    columns.append({
                        "name":        col["name"],
                        "type":        str(col["type"]),
                        "nullable":    col.get("nullable", True),
                        "primary_key": False,
                        "default":     str(col.get("default", "")),
                    })

                # Primary keys
                try:
                    pk_cols = inspector.get_pk_constraint(tbl).get("constrained_columns", [])
                    for col in columns:
                        if col["name"] in pk_cols:
                            col["primary_key"] = True
                except Exception:
                    pass

                # Foreign keys
                foreign_keys = []
                try:
                    for fk in inspector.get_foreign_keys(tbl):
                        for col in fk.get("constrained_columns", []):
                            foreign_keys.append({
                                "column":            col,
                                "references_table":  fk.get("referred_table", ""),
                                "references_column": fk.get("referred_columns", [""])[0],
                            })
                except Exception:
                    pass

                # Row count
                try:
                    row_count = conn.execute(text(f"SELECT COUNT(*) FROM {tbl}")).scalar()
                except Exception:
                    row_count = 0

                schema["tables"][tbl] = {
                    "columns":      columns,
                    "foreign_keys": foreign_keys,
                    "row_count":    row_count,
                }

        return {"success": True, "schema": schema}

    except Exception as e:
        return {"success": False, "error": str(e)}


# ------------------------------------------------------------------
# Tool 1B: search_schema()
# ------------------------------------------------------------------
def _add_join_path_tables(tables: set, session_id: str = "default") -> set:
    """
    Adds the intermediate tables needed to join the selected tables,
    using foreign keys from the live schema. Works on any database.
    """
    from collections import deque

    if len(tables) < 2:
        return tables
    schema_result = get_schema(session_id=session_id)
    if not schema_result["success"]:
        return tables
    all_tables = schema_result["schema"]["tables"]

    graph = {t: set() for t in all_tables}
    for t, info in all_tables.items():
        for fk in info.get("foreign_keys", []):
            ref = fk.get("references_table")
            if ref in graph:
                graph[t].add(ref)
                graph[ref].add(t)

    def shortest_path(start, goal):
        queue, seen = deque([[start]]), {start}
        while queue:
            path = queue.popleft()
            if path[-1] == goal:
                return path
            for nxt in sorted(graph.get(path[-1], ())):
                if nxt not in seen:
                    seen.add(nxt)
                    queue.append(path + [nxt])
        return []

    selected = sorted(t for t in tables if t in graph)
    expanded = set(tables)
    for t in selected[1:]:
        expanded.update(shortest_path(selected[0], t))
    return expanded

def search_schema(question: str, session_id: str = "default") -> dict:
    """
    Finds the most relevant tables for a natural language question.
    Uses keyword matching against table names and column names.
    Falls back to dynamic table discovery for unknown schemas.
    """

    # Keyword → table mapping (Northwind defaults)
    TABLE_KEYWORDS = {
        "Artist":        ["artist", "band", "musician", "performer", "who made", "singer"],
        "Album":         ["album", "record", "release", "collection", "disc"],
        "Track":         ["track", "song", "music", "audio", "duration", "media", "price"],
        "Genre":         ["genre", "type", "style", "category", "kind", "rock", "jazz", "latin", "metal", "blues"],
        "Invoice":       ["invoice", "order", "purchase", "sale", "bought", "transaction", "revenue", "total", "billing"],
        "InvoiceLine":   ["line item", "quantity", "unit price", "item", "revenue", "earning", "income"],
        "Customer":      ["customer", "client", "buyer", "who bought", "contact", "email", "country", "city"],
        "Employee":      ["employee", "staff", "rep", "manager", "reports to", "hire", "title"],
        "Playlist":      ["playlist", "collection", "list", "queue"],
        "PlaylistTrack": ["playlist track", "track in playlist", "song in list"],
        "MediaType":     ["media", "format", "mp3", "aac", "wav", "file type"],
    }

    VIEW_KEYWORDS = {}

    question_lower  = question.lower()
    relevant_tables = set()
    relevant_views  = set()

    # First: try keyword matching
    for table, keywords in TABLE_KEYWORDS.items():
        if any(kw in question_lower for kw in keywords):
            relevant_tables.add(table)

    for view, keywords in VIEW_KEYWORDS.items():
        if any(kw in question_lower for kw in keywords):
            relevant_views.add(view)

    # If no keyword match: get all tables from the actual database
    # This handles unknown schemas (user's own database)
    if not relevant_tables:
        schema_result = get_schema(session_id=session_id)
        if schema_result["success"]:
            all_tables = list(schema_result["schema"]["tables"].keys())
            # Return first 5 tables as default context
            relevant_tables = set(all_tables[:5])
            
    relevant_tables = _add_join_path_tables(relevant_tables, session_id)

    # Get schemas for relevant tables
    result = {}
    for table in relevant_tables:
        table_schema = get_schema(table_name=table, session_id=session_id)
        if table_schema["success"] and table in table_schema["schema"]["tables"]:
            result[table] = table_schema["schema"]["tables"][table]

    return {
        "success":         True,
        "question":        question,
        "relevant_tables": list(relevant_tables),
        "relevant_views":  list(relevant_views),
        "schemas":         result,
    }


# ------------------------------------------------------------------
# Tool 1C: get_table_sample()
# ------------------------------------------------------------------

def get_table_sample(
    table_name: str,
    limit:      int = 3,
    session_id: str = "default",
) -> dict:
    """
    Returns sample rows from a table.
    Works with PostgreSQL, MySQL, and SQLite.
    """
    from sqlalchemy import text

    limit = min(limit, 5)

    try:
        engine = _get_engine(session_id)

        with engine.connect() as conn:
            result = conn.execute(text(f"SELECT * FROM {table_name} LIMIT {limit}"))
            rows    = result.fetchall()
            columns = list(result.keys())

        return {
            "success":     True,
            "table":       table_name,
            "columns":     columns,
            "sample_rows": [dict(zip(columns, row)) for row in rows],
        }

    except Exception as e:
        return {"success": False, "error": str(e)}


# ------------------------------------------------------------------
# Quick self-test
# ------------------------------------------------------------------

if __name__ == "__main__":

    print("=" * 60)
    print("TEST 1: get_schema() — all tables")
    print("=" * 60)
    result = get_schema()
    if result["success"]:
        for tbl, info in result["schema"]["tables"].items():
            col_names = [c["name"] for c in info["columns"]]
            print(f"  {tbl:20s} | rows: {info['row_count']:4d} | columns: {col_names}")
        print(f"  Views: {result['schema']['views']}")
    else:
        print(f"  ERROR: {result['error']}")

    print("\n" + "=" * 60)
    print("TEST 2: search_schema() — 'top selling products'")
    print("=" * 60)
    result = search_schema("What are the top selling products by revenue?")
    if result["success"]:
        print(f"  Relevant tables : {result['relevant_tables']}")
        print(f"  Relevant views  : {result['relevant_views']}")
    else:
        print(f"  ERROR: {result['error']}")

    print("\n" + "=" * 60)
    print("TEST 3: get_table_sample() — Customer")
    print("=" * 60)
    result = get_table_sample("Customer", limit=3)
    if result["success"]:
        print(f"  Columns: {result['columns']}")
        for row in result["sample_rows"]:
            print(f"  {row}")
    else:
        print(f"  ERROR: {result['error']}")