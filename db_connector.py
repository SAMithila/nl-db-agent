"""
db_connector.py
---------------
Dynamic Database Connector

Handles connections to PostgreSQL, MySQL, and SQLite.
This is the core upgrade that turns the agent from a demo
into a real product — users bring their own database.

Supported connection strings:
    SQLite     → sqlite:///path/to/db.db
    PostgreSQL → postgresql://user:pass@host:5432/dbname
    MySQL      → mysql+pymysql://user:pass@host:3306/dbname

Functions:
    connect()           → create and validate a DB connection
    get_engine()        → get SQLAlchemy engine for a connection string
    test_connection()   → verify a connection string works
    get_db_type()       → detect database type from connection string
"""

import os
import tempfile
import sqlite3
import shutil
from typing import Optional
from sqlalchemy import create_engine, text, inspect
from sqlalchemy.engine import Engine
from sqlalchemy.exc import OperationalError, ArgumentError
from fastapi import UploadFile, File, Form



# ------------------------------------------------------------------
# Default connection (Northwind SQLite — for demo)
# ------------------------------------------------------------------

DEFAULT_DB = f"sqlite:///{os.path.join(os.path.dirname(__file__), 'db/chinook.db')}"


# ------------------------------------------------------------------
# Connection registry — stores active connections per session
# In production this would be Redis or a proper session store
# ------------------------------------------------------------------

_connections: dict = {}


# ------------------------------------------------------------------
# Core functions
# ------------------------------------------------------------------

def get_db_type(connection_string: str) -> str:
    """
    Detects database type from connection string.

    Returns: 'sqlite' | 'postgresql' | 'mysql' | 'unknown'
    """
    cs = connection_string.lower()
    if cs.startswith("sqlite"):
        return "sqlite"
    elif cs.startswith("postgresql") or cs.startswith("postgres"):
        return "postgresql"
    elif cs.startswith("mysql"):
        return "mysql"
    return "unknown"


def get_engine(connection_string: str) -> Engine:
    """
    Creates a SQLAlchemy engine for any supported database.

    Args:
        connection_string: Full database URL

    Returns:
        SQLAlchemy Engine
    """
    db_type = get_db_type(connection_string)

    # SQLite — use check_same_thread=False for multi-threaded FastAPI
    if db_type == "sqlite":
        return create_engine(
            connection_string,
            connect_args={"check_same_thread": False},
        )

    # PostgreSQL
    elif db_type == "postgresql":
        # Normalize postgres:// → postgresql://
        cs = connection_string.replace("postgres://", "postgresql://")
        return create_engine(cs, pool_pre_ping=True)

    # MySQL
    elif db_type == "mysql":
        # Ensure pymysql driver is specified
        if "pymysql" not in connection_string:
            cs = connection_string.replace("mysql://", "mysql+pymysql://")
        else:
            cs = connection_string
        return create_engine(cs, pool_pre_ping=True)

    else:
        raise ValueError(f"Unsupported database type: {connection_string[:20]}")


def test_connection(connection_string: str) -> dict:
    """
    Tests if a connection string is valid and reachable.

    Args:
        connection_string: Full database URL

    Returns:
        dict with success status, db_type, and error if any
    """
    try:
        engine  = get_engine(connection_string)
        db_type = get_db_type(connection_string)

        # Test with a simple query
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))

        # Get table count
        inspector   = inspect(engine)
        table_count = len(inspector.get_table_names())

        return {
            "success":     True,
            "db_type":     db_type,
            "table_count": table_count,
            "message":     f"Connected successfully — {table_count} tables found",
        }

    except OperationalError as e:
        return {
            "success": False,
            "error":   f"Connection failed: {str(e)[:200]}",
            "hint":    _get_connection_hint(connection_string),
        }
    except ArgumentError as e:
        return {
            "success": False,
            "error":   f"Invalid connection string: {str(e)[:200]}",
            "hint":    _get_connection_hint(connection_string),
        }
    except Exception as e:
        return {
            "success": False,
            "error":   str(e)[:200],
            "hint":    _get_connection_hint(connection_string),
        }


def connect(
    connection_string: str,
    session_id: str = "default",
) -> dict:
    """
    Connects to a database and stores the engine for reuse.

    Args:
        connection_string: Full database URL
        session_id:        Session identifier (per user/request)

    Returns:
        dict with success status and connection info
    """
    # Test first
    test = test_connection(connection_string)
    if not test["success"]:
        return test

    # Store engine
    engine = get_engine(connection_string)
    _connections[session_id] = {
        "engine":            engine,
        "connection_string": connection_string,
        "db_type":           test["db_type"],
        "table_count":       test["table_count"],
    }

    return {
        "success":     True,
        "session_id":  session_id,
        "db_type":     test["db_type"],
        "table_count": test["table_count"],
        "message":     test["message"],
    }


def get_active_engine(session_id: str = "default") -> Engine:
    """
    Returns the active engine for a session.
        Returns the engine for a connected session. Demo mode ("default")
    uses the Chinook database; an unknown named session raises LookupError.

    Args:
        session_id: Session identifier

    Returns:
        SQLAlchemy Engine
    """
    if session_id in _connections:
        return _connections[session_id]["engine"]

    if session_id != "default":
        raise LookupError(
            f"No active database connection for session '{session_id}'. "
            "It may have expired after a server restart — please reconnect."
        )
    
    # Fallback to default demo database
    return get_engine(DEFAULT_DB)


def get_connection_info(session_id: str = "default") -> dict:
    """Returns info about the active connection for a session."""
    if session_id in _connections:
        info = _connections[session_id]
        return {
            "connected":   True,
            "db_type":     info["db_type"],
            "table_count": info["table_count"],
        }
    return {
        "connected": False,
        "db_type":   "sqlite",
        "message":   "Using default demo database (Northwind)",
    }


def disconnect(session_id: str = "default") -> None:
    """Removes a session's database connection."""
    if session_id in _connections:
        _connections[session_id]["engine"].dispose()
        del _connections[session_id]


# ------------------------------------------------------------------
# Helper: connection string hints
# ------------------------------------------------------------------

def _get_connection_hint(connection_string: str) -> str:
    """Returns a helpful hint based on the connection string format."""
    db_type = get_db_type(connection_string)

    hints = {
        "postgresql": "Format: postgresql://username:password@host:5432/dbname",
        "mysql":      "Format: mysql+pymysql://username:password@host:3306/dbname",
        "sqlite":     "Format: sqlite:///path/to/database.db",
        "unknown":    "Supported: postgresql://, mysql://, sqlite:///",
    }
    return hints.get(db_type, hints["unknown"])


# ------------------------------------------------------------------
# Quick self-test
# ------------------------------------------------------------------

if __name__ == "__main__":

    print("=" * 60)
    print("DB CONNECTOR TESTS")
    print("=" * 60)

    # Test 1: SQLite (default Northwind)
    print("\n── TEST 1: SQLite (Northwind) ──")
    result = test_connection(DEFAULT_DB)
    status = "✅" if result["success"] else "❌"
    print(f"{status} {result.get('message') or result.get('error')}")
    if result["success"]:
        print(f"   DB type : {result['db_type']}")
        print(f"   Tables  : {result['table_count']}")

    # Test 2: Invalid connection string
    print("\n── TEST 2: Invalid connection ──")
    result = test_connection("postgresql://fake:fake@localhost:5432/fake")
    status = "✅" if not result["success"] else "❌"
    print(f"{status} Correctly rejected invalid connection")
    print(f"   Hint: {result.get('hint')}")

    # Test 3: connect() and get_active_engine()
    print("\n── TEST 3: connect() and get_active_engine() ──")
    result = connect(DEFAULT_DB, session_id="test_session")
    status = "✅" if result["success"] else "❌"
    print(f"{status} {result.get('message') or result.get('error')}")

    engine = get_active_engine("test_session")
    print(f"   Engine  : {engine}")

    # Test 4: get_db_type()
    print("\n── TEST 4: get_db_type() ──")
    cases = [
        ("sqlite:///test.db",                          "sqlite"),
        ("postgresql://u:p@host:5432/db",              "postgresql"),
        ("postgres://u:p@host:5432/db",                "postgresql"),
        ("mysql+pymysql://u:p@host:3306/db",           "mysql"),
    ]
    for cs, expected in cases:
        detected = get_db_type(cs)
        status   = "✅" if detected == expected else "❌"
        print(f"   {status} {cs[:40]:40s} → {detected}")

    # Test 5: disconnect()
    print("\n── TEST 5: disconnect() ──")
    disconnect("test_session")
    info = get_connection_info("test_session")
    status = "✅" if not info["connected"] else "❌"
    print(f"{status} Disconnected successfully")

    print("\n" + "=" * 60)
    print("✅ DB connector tests complete")
    print("=" * 60)