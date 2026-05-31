from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT_DIR = Path(__file__).resolve().parents[2]
DEFAULT_DB_PATH = ROOT_DIR / ".data" / "assessment.sqlite"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def get_db_path() -> Path:
    return Path(os.getenv("ASSESSMENT_DB_PATH", str(DEFAULT_DB_PATH)))


def connect(db_path: str | Path | None = None) -> sqlite3.Connection:
    path = Path(db_path) if db_path else get_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS users (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            roles_json TEXT NOT NULL,
            permissions_json TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS tasks (
            id TEXT PRIMARY KEY,
            created_by TEXT NOT NULL,
            title TEXT NOT NULL,
            prompt TEXT NOT NULL,
            status TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(created_by) REFERENCES users(id)
        );

        CREATE TABLE IF NOT EXISTS runs (
            id TEXT PRIMARY KEY,
            task_id TEXT NOT NULL,
            requested_by TEXT NOT NULL,
            status TEXT NOT NULL,
            result_json TEXT,
            error TEXT,
            token_cost INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            started_at TEXT,
            finished_at TEXT,
            FOREIGN KEY(task_id) REFERENCES tasks(id),
            FOREIGN KEY(requested_by) REFERENCES users(id)
        );

        CREATE TABLE IF NOT EXISTS run_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT NOT NULL,
            seq INTEGER NOT NULL,
            type TEXT NOT NULL,
            tool_name TEXT,
            payload_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY(run_id) REFERENCES runs(id)
        );

        CREATE TABLE IF NOT EXISTS audit_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            actor_id TEXT NOT NULL,
            action TEXT NOT NULL,
            resource TEXT NOT NULL,
            decision TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS knowledge_chunks (
            id TEXT PRIMARY KEY,
            doc_id TEXT NOT NULL,
            source_path TEXT NOT NULL,
            title TEXT NOT NULL,
            permission TEXT NOT NULL,
            content TEXT NOT NULL,
            embedding_json TEXT
        );
        """
    )
    conn.commit()


def reset_tables(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        DELETE FROM run_events;
        DELETE FROM audit_logs;
        DELETE FROM runs;
        DELETE FROM tasks;
        DELETE FROM knowledge_chunks;
        DELETE FROM users;
        """
    )
    conn.commit()


def encode_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def decode_json(value: str | None, default: Any = None) -> Any:
    if value is None:
        return default
    return json.loads(value)


def row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return dict(row)


def next_event_seq(conn: sqlite3.Connection, run_id: str) -> int:
    row = conn.execute(
        "SELECT COALESCE(MAX(seq), 0) + 1 AS next_seq FROM run_events WHERE run_id = ?",
        (run_id,),
    ).fetchone()
    return int(row["next_seq"])


def insert_run_event(
    conn: sqlite3.Connection,
    run_id: str,
    event_type: str,
    payload: dict[str, Any],
    tool_name: str | None = None,
) -> None:
    from agentops_assessment.security.redaction import redact

    conn.execute(
        """
        INSERT INTO run_events (run_id, seq, type, tool_name, payload_json, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            run_id,
            next_event_seq(conn, run_id),
            event_type,
            tool_name,
            encode_json(redact(payload)),
            now_iso(),
        ),
    )
    conn.commit()


def insert_audit_log(
    conn: sqlite3.Connection,
    actor_id: str,
    action: str,
    resource: str,
    payload: dict[str, Any],
    decision: str = "allow",
) -> None:
    from agentops_assessment.security.redaction import redact

    conn.execute(
        """
        INSERT INTO audit_logs (actor_id, action, resource, decision, payload_json, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (actor_id, action, resource, decision, encode_json(redact(payload)), now_iso()),
    )
    conn.commit()
