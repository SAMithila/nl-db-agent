"""
main.py
-------
FastAPI Backend — NL→DB Agent API

Exposes the agent as a REST API with:
    POST /query          → run a natural language query
    GET  /health         → health check
    GET  /schema         → get database schema
    GET  /metrics        → get evaluation metrics
    GET  /traces         → list recent agent traces

Designed for AWS deployment:
    - Environment-based configuration
    - CORS enabled for Streamlit frontend
    - Structured JSON responses
    - Request/response logging
"""

import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from pathlib import Path
from dotenv import load_dotenv
load_dotenv(Path(__file__).parent.parent / ".env")

from fastapi import FastAPI, HTTPException, Header, UploadFile, File, Form
import tempfile, shutil, sqlite3
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional
import time
from datetime import datetime

from agent.graph              import run_query
from observability.tracer     import save_trace, list_traces, get_metrics_summary
from tools.schema_inspector   import get_schema
from evaluation.metrics       import load_results
from db_connector             import connect, get_connection_info, disconnect, test_connection


# ------------------------------------------------------------------
# App setup
# ------------------------------------------------------------------

app = FastAPI(
    title       = "NL→DB Agent API",
    description = "Natural language to database query agent",
    version     = "1.0.0",
)

# CORS — allow frontend to connect
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://llm-sql-agent-ui.vercel.app",
        "http://localhost:3000",
        "*",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ------------------------------------------------------------------
# Request/Response models
# ------------------------------------------------------------------

class QueryRequest(BaseModel):
    question: str
    user_id:  str = "default_user"
    session_id: str = "default"

class FeedbackRequest(BaseModel):
    message_id: str
    question:   str
    route:      str
    rating:     int        # 1 = thumbs up, -1 = thumbs down
    session_id: str = "default"

class QueryResponse(BaseModel):
    success:              bool
    question:             str
    answer:               Optional[str]    = None
    key_insights:         Optional[list]   = None
    sql:                  Optional[str]    = None
    row_count:            Optional[int]    = None
    execution_ms:         Optional[int]    = None
    columns:              Optional[list]   = None      # ADD THIS
    rows:                 Optional[list]   = None 
    needs_clarification:  bool             = False
    clarification:        Optional[dict]   = None
    error:                Optional[str]    = None
    trace_summary:        Optional[list]   = None
    timestamp:            str              = ""
    route:                Optional[str]    = None
    sources:              Optional[list]   = None
    route_reason:         Optional[str]    = None


# ------------------------------------------------------------------
# Routes
# ------------------------------------------------------------------

@app.get("/health")
def health_check():
    """Health check endpoint for AWS load balancer."""
    return {
        "status":    "healthy",
        "timestamp": datetime.now().isoformat(),
        "version":   "1.0.0",
    }


@app.post("/query", response_model=QueryResponse)
def query(request: QueryRequest):
    """
    Main endpoint — run a natural language query through the agent.

    Request body:
        {
            "question": "Who are our top 5 customers?",
            "user_id":  "alice"
        }

    Returns structured response with answer, SQL, and metadata.
    """
    if not request.question.strip():
        raise HTTPException(status_code=400, detail="Question cannot be empty")

    start = time.time()

    try:
        state = run_query(request.question, request.user_id, request.session_id)

        # Save trace
        save_trace(state)

        elapsed_ms = round((time.time() - start) * 1000)

        # Clarification needed
        if state.needs_clarification:
            return QueryResponse(
                success             = False,
                question            = request.question,
                needs_clarification = True,
                clarification       = state.clarification_response,
                timestamp           = datetime.now().isoformat(),
            )

        # Check success — RAG route doesn't use execution_success
        final    = state.final_response or {}
        exec_res = state.execution_result or {}
        
        # Determine success: SQL needs execution_success, RAG uses final_response success
        is_success = final.get("success", False)

        # Error case
        if not is_success:
            return QueryResponse(
                success   = False,
                question  = request.question,
                error     = final.get("error_message") or state.error,
                timestamp = datetime.now().isoformat(),
            )

        # Success — works for SQL, RAG, and BOTH routes
        return QueryResponse(
            success      = True,
            question     = request.question,
            answer       = final.get("summary", ""),
            key_insights = final.get("key_insights", []),
            sql          = state.generated_sql,
            row_count    = exec_res.get("row_count", 0),
            execution_ms = exec_res.get("execution_ms", 0),
            columns      = exec_res.get("columns", []),
            rows         = exec_res.get("rows", []),
            route        = final.get("route") or state.route,   # ADD
            route_reason = state.route_reason,
            sources      = final.get("sources", []),             # ADD
            trace_summary = [
                {"node": t["node"], "message": t["message"]}
                for t in state.trace
            ],
            timestamp    = datetime.now().isoformat(),
        )
        

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/schema")
def schema():
    """Returns the full database schema."""
    result = get_schema()
    if not result["success"]:
        raise HTTPException(status_code=500, detail=result["error"])
    return result["schema"]


@app.get("/metrics")
def metrics():
    """Returns the latest evaluation metrics."""
    return get_metrics_summary()


@app.get("/traces")
def traces(limit: int = 10):
    """Returns recent agent traces."""
    return {"traces": list_traces(limit=limit)}

class ConnectRequest(BaseModel):
    connection_string: str
    session_id: str = "default"


@app.post("/connect")
def connect_database(request: ConnectRequest):
    """
    Connect to a user's database.
    
    Request body:
        {
            "connection_string": "postgresql://user:pass@host:5432/dbname",
            "session_id": "user123"
        }
    """
    if not request.connection_string.strip():
        raise HTTPException(status_code=400, detail="Connection string cannot be empty")
    
    result = connect(request.connection_string, request.session_id)
    
    if not result["success"]:
        raise HTTPException(status_code=400, detail=result.get("error", "Connection failed"))
    
    return result


@app.get("/connection/{session_id}")
def connection_status(session_id: str = "default"):
    """Returns the current connection status for a session."""
    return get_connection_info(session_id)

@app.post("/disconnect/{session_id}")
def disconnect_database(session_id: str = "default"):
    """Disconnects a session from its database."""
    disconnect(session_id)
    return {"success": True, "message": "Disconnected"}

@app.post("/feedback")
def submit_feedback(request: FeedbackRequest):
    """Stores user feedback for human baseline calibration."""
    import json
    from pathlib import Path
    from datetime import datetime
    
    feedback_path = Path("observability/feedback.jsonl")
    feedback_path.parent.mkdir(exist_ok=True)
    
    entry = {
        "message_id": request.message_id,
        "question":   request.question,
        "route":      request.route,
        "rating":     request.rating,
        "session_id": request.session_id,
        "timestamp":  datetime.now().isoformat(),
    }
    
    with open(feedback_path, "a") as f:
        f.write(json.dumps(entry) + "\n")
    
    return {"success": True, "message": "Feedback recorded"}

@app.get("/feedback/summary")
def feedback_summary():
    """Returns aggregated feedback stats for evaluation dashboard."""
    from pathlib import Path
    
    feedback_path = Path("observability/feedback.jsonl")
    if not feedback_path.exists():
        return {"total": 0, "thumbs_up": 0, "thumbs_down": 0, "by_route": {}}
    
    entries = []
    with open(feedback_path) as f:
        for line in f:
            try: entries.append(json.loads(line))
            except: pass
    
    thumbs_up   = sum(1 for e in entries if e.get("rating") == 1)
    thumbs_down = sum(1 for e in entries if e.get("rating") == -1)
    
    by_route = {}
    for e in entries:
        route = e.get("route", "unknown")
        if route not in by_route:
            by_route[route] = {"thumbs_up": 0, "thumbs_down": 0}
        if e.get("rating") == 1:
            by_route[route]["thumbs_up"] += 1
        else:
            by_route[route]["thumbs_down"] += 1
    
    return {
        "total":       len(entries),
        "thumbs_up":   thumbs_up,
        "thumbs_down": thumbs_down,
        "satisfaction_rate": round(thumbs_up / len(entries) * 100, 1) if entries else 0,
        "by_route":    by_route,
    }

@app.post("/connect/sqlite-upload")
async def connect_sqlite_upload(
    file: UploadFile = File(...),
    session_id: str = Form(default="default")
):
    """Upload a SQLite .db file and connect to it."""
    if not (file.filename.endswith(".db") or file.filename.endswith(".sqlite")):
        raise HTTPException(status_code=400, detail="File must be .db or .sqlite")

    tmp_dir = tempfile.mkdtemp()
    tmp_path = os.path.join(tmp_dir, "uploaded.db")

    try:
        with open(tmp_path, "wb") as f:
            shutil.copyfileobj(file.file, f)

        conn = sqlite3.connect(tmp_path)
        cursor = conn.cursor()
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")        
        tables = [row[0] for row in cursor.fetchall()]

        table_details = {}
        for table in tables:
            cursor.execute(f"PRAGMA table_info(`{table}`)")
            columns = [{"name": row[1], "type": row[2]} for row in cursor.fetchall()]
            cursor.execute(f"SELECT COUNT(*) FROM `{table}`")
            row_count = cursor.fetchone()[0]
            table_details[table] = {"columns": columns, "row_count": row_count}

        conn.close()

        result = connect(f"sqlite:///{tmp_path}", session_id)
        if not result["success"]:
            raise HTTPException(status_code=400, detail=result.get("error"))

        return {
            "success":     True,
            "db_type":     "sqlite",
            "filename":    file.filename,
            "session_id":  session_id,
            "table_count": len(tables),
            "tables":      table_details,
        }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to process file: {str(e)}")


# ------------------------------------------------------------------
# Run locally
# ------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "api.main:app",
        host    = "0.0.0.0",
        port    = int(os.getenv("PORT", 8000)),
        reload  = True,
    )