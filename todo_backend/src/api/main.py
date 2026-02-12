from __future__ import annotations

import os
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException, Path as FPath, Query, Response, status
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, ConfigDict


def _utc_now_iso() -> str:
    """Return the current UTC time in ISO-8601 format with 'Z' suffix."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _parse_db_path_from_db_connection_txt(contents: str) -> Optional[str]:
    """
    Parse SQLite DB file path from db_connection.txt contents.

    Expected to contain a line like:
      # File path: /abs/path/to/myapp.db
    """
    for line in contents.splitlines():
        m = re.match(r"^\s*#\s*File path:\s*(.+?)\s*$", line)
        if m:
            return m.group(1).strip()
    return None


def _load_sqlite_db_path() -> str:
    """
    Load SQLite DB path.

    Source of truth:
      - database container file: ../simple-to-do-list-281266-281277/database/db_connection.txt

    Fallback:
      - SQLITE_DB environment variable (optional)
    """
    # Try env var first (useful in deployment), but db_connection.txt is source of truth for this repo.
    env_db = os.getenv("SQLITE_DB")
    if env_db:
        return env_db

    # Compute absolute path to the db_connection.txt in sibling workspace.
    # This backend file is at: <repo>/simple-to-do-list-281266-281275/todo_backend/src/api/main.py
    # Database file is at: <repo>/simple-to-do-list-281266-281277/database/db_connection.txt
    here = Path(__file__).resolve()
    repo_root = here.parents[5]  # .../code-generation
    db_conn_txt = repo_root / "simple-to-do-list-281266-281277" / "database" / "db_connection.txt"
    if not db_conn_txt.exists():
        raise RuntimeError(
            f"db_connection.txt not found at expected path: {db_conn_txt}. "
            "Ensure the database container workspace exists."
        )

    contents = db_conn_txt.read_text(encoding="utf-8")
    parsed = _parse_db_path_from_db_connection_txt(contents)
    if not parsed:
        raise RuntimeError(
            f"Could not parse SQLite DB file path from {db_conn_txt}. "
            "Expected a line like '# File path: /abs/path/to/myapp.db'."
        )
    return parsed


SQLITE_DB_PATH = _load_sqlite_db_path()


@contextmanager
def _db() -> sqlite3.Connection:
    """
    Context manager for a SQLite connection with Row mapping enabled.
    """
    conn = sqlite3.connect(SQLITE_DB_PATH)
    try:
        conn.row_factory = sqlite3.Row
        # Helps in case multiple processes hit the DB; safe default.
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA foreign_keys=ON;")
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _ensure_schema() -> None:
    """
    Ensure todos table exists.

    This is defensive in case init_db.py wasn't run yet.
    """
    with _db() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS todos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                completed INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            """
        )


def _row_to_todo(row: sqlite3.Row) -> Dict[str, Any]:
    """Convert sqlite Row -> JSON-serializable todo dict."""
    return {
        "id": int(row["id"]),
        "title": str(row["title"]),
        "completed": bool(row["completed"]),
        "created_at": str(row["created_at"]),
        "updated_at": str(row["updated_at"]),
    }


class TodoBase(BaseModel):
    """Common fields for todo payloads."""

    model_config = ConfigDict(extra="forbid")

    title: str = Field(..., min_length=1, max_length=200, description="Todo title")
    completed: bool = Field(False, description="Completion state")


class TodoCreate(BaseModel):
    """Request model for creating a todo."""

    model_config = ConfigDict(extra="forbid")

    title: str = Field(..., min_length=1, max_length=200, description="Todo title")


class TodoUpdate(BaseModel):
    """Request model for updating a todo."""

    model_config = ConfigDict(extra="forbid")

    title: Optional[str] = Field(None, min_length=1, max_length=200, description="Todo title")
    completed: Optional[bool] = Field(None, description="Completion state")


class TodoOut(TodoBase):
    """Response model for a todo."""

    id: int = Field(..., description="Todo id")
    created_at: str = Field(..., description="ISO-8601 timestamp (UTC)")
    updated_at: str = Field(..., description="ISO-8601 timestamp (UTC)")


class TodoListResponse(BaseModel):
    """Response model for listing todos."""

    model_config = ConfigDict(extra="forbid")

    items: List[TodoOut] = Field(..., description="List of todos")
    total: int = Field(..., ge=0, description="Total count returned")


openapi_tags = [
    {"name": "Health", "description": "Service health and diagnostics."},
    {"name": "Todos", "description": "CRUD operations for todo items."},
]

app = FastAPI(
    title="Todo Backend API",
    description="FastAPI REST API for a simple todo application backed by SQLite.",
    version="1.0.0",
    openapi_tags=openapi_tags,
)

# CORS: keep permissive by default to be compatible with the React frontend in dev.
# If you want to restrict later, provide a comma-separated list in ALLOW_ORIGINS.
allow_origins_env = os.getenv("ALLOW_ORIGINS", "*")
allow_origins = ["*"] if allow_origins_env.strip() == "*" else [o.strip() for o in allow_origins_env.split(",") if o.strip()]

app.add_middleware(
    CORSMiddleware,
    allow_origins=allow_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
def _startup() -> None:
    """Initialize database schema on startup."""
    _ensure_schema()


# PUBLIC_INTERFACE
@app.get("/", tags=["Health"], summary="Health check", operation_id="health_check")
def health_check() -> Dict[str, str]:
    """Return a simple health check message."""
    return {"message": "Healthy"}


# PUBLIC_INTERFACE
@app.get(
    "/todos",
    tags=["Todos"],
    response_model=TodoListResponse,
    summary="List todos",
    operation_id="list_todos",
)
def list_todos(
    completed: Optional[bool] = Query(None, description="Filter by completion status"),
    q: Optional[str] = Query(None, min_length=1, max_length=200, description="Search substring in title (case-insensitive)"),
    limit: int = Query(100, ge=1, le=500, description="Max number of todos to return"),
    offset: int = Query(0, ge=0, description="Offset for pagination"),
) -> TodoListResponse:
    """List todos with optional filtering, search, and pagination."""
    where = []
    params: List[Any] = []

    if completed is not None:
        where.append("completed = ?")
        params.append(1 if completed else 0)

    if q:
        where.append("LOWER(title) LIKE ?")
        params.append(f"%{q.lower()}%")

    where_sql = f"WHERE {' AND '.join(where)}" if where else ""
    sql = f"""
        SELECT id, title, completed, created_at, updated_at
        FROM todos
        {where_sql}
        ORDER BY id DESC
        LIMIT ? OFFSET ?;
    """
    params.extend([limit, offset])

    with _db() as conn:
        rows = conn.execute(sql, params).fetchall()

    items = [_row_to_todo(r) for r in rows]
    return TodoListResponse(items=items, total=len(items))


# PUBLIC_INTERFACE
@app.post(
    "/todos",
    tags=["Todos"],
    response_model=TodoOut,
    status_code=status.HTTP_201_CREATED,
    summary="Create a todo",
    operation_id="create_todo",
)
def create_todo(payload: TodoCreate) -> Dict[str, Any]:
    """Create a new todo with the given title."""
    now = _utc_now_iso()
    with _db() as conn:
        cur = conn.execute(
            """
            INSERT INTO todos (title, completed, created_at, updated_at)
            VALUES (?, 0, ?, ?);
            """,
            (payload.title.strip(), now, now),
        )
        todo_id = int(cur.lastrowid)
        row = conn.execute(
            """
            SELECT id, title, completed, created_at, updated_at
            FROM todos
            WHERE id = ?;
            """,
            (todo_id,),
        ).fetchone()

    return _row_to_todo(row)


def _get_todo_or_404(conn: sqlite3.Connection, todo_id: int) -> sqlite3.Row:
    row = conn.execute(
        """
        SELECT id, title, completed, created_at, updated_at
        FROM todos
        WHERE id = ?;
        """,
        (todo_id,),
    ).fetchone()
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Todo not found")
    return row


# PUBLIC_INTERFACE
@app.put(
    "/todos/{todo_id}",
    tags=["Todos"],
    response_model=TodoOut,
    summary="Update a todo (replace semantics)",
    operation_id="update_todo_put",
)
def update_todo_put(
    todo_id: int = FPath(..., ge=1, description="Todo id"),
    payload: TodoBase = ...,
) -> Dict[str, Any]:
    """
    Fully update a todo.

    This endpoint expects both `title` and `completed`.
    """
    now = _utc_now_iso()
    with _db() as conn:
        _get_todo_or_404(conn, todo_id)
        conn.execute(
            """
            UPDATE todos
            SET title = ?, completed = ?, updated_at = ?
            WHERE id = ?;
            """,
            (payload.title.strip(), 1 if payload.completed else 0, now, todo_id),
        )
        row = _get_todo_or_404(conn, todo_id)

    return _row_to_todo(row)


# PUBLIC_INTERFACE
@app.patch(
    "/todos/{todo_id}",
    tags=["Todos"],
    response_model=TodoOut,
    summary="Update a todo (partial)",
    operation_id="update_todo_patch",
)
def update_todo_patch(
    todo_id: int = FPath(..., ge=1, description="Todo id"),
    payload: TodoUpdate = ...,
) -> Dict[str, Any]:
    """Partially update a todo (title and/or completed)."""
    if payload.title is None and payload.completed is None:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="At least one field must be provided")

    now = _utc_now_iso()
    with _db() as conn:
        existing = _get_todo_or_404(conn, todo_id)
        new_title = payload.title.strip() if payload.title is not None else str(existing["title"])
        new_completed = payload.completed if payload.completed is not None else bool(existing["completed"])

        conn.execute(
            """
            UPDATE todos
            SET title = ?, completed = ?, updated_at = ?
            WHERE id = ?;
            """,
            (new_title, 1 if new_completed else 0, now, todo_id),
        )
        row = _get_todo_or_404(conn, todo_id)

    return _row_to_todo(row)


# PUBLIC_INTERFACE
@app.post(
    "/todos/{todo_id}/toggle",
    tags=["Todos"],
    response_model=TodoOut,
    summary="Toggle completion",
    operation_id="toggle_todo_complete",
)
def toggle_todo_complete(todo_id: int = FPath(..., ge=1, description="Todo id")) -> Dict[str, Any]:
    """Toggle a todo's completed state."""
    now = _utc_now_iso()
    with _db() as conn:
        existing = _get_todo_or_404(conn, todo_id)
        new_completed = 0 if int(existing["completed"]) == 1 else 1

        conn.execute(
            """
            UPDATE todos
            SET completed = ?, updated_at = ?
            WHERE id = ?;
            """,
            (new_completed, now, todo_id),
        )
        row = _get_todo_or_404(conn, todo_id)

    return _row_to_todo(row)


# PUBLIC_INTERFACE
@app.delete(
    "/todos/{todo_id}",
    tags=["Todos"],
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a todo",
    operation_id="delete_todo",
)
def delete_todo(todo_id: int = FPath(..., ge=1, description="Todo id")) -> Response:
    """Delete a todo by id."""
    with _db() as conn:
        _get_todo_or_404(conn, todo_id)
        conn.execute("DELETE FROM todos WHERE id = ?;", (todo_id,))
    return Response(status_code=status.HTTP_204_NO_CONTENT)
