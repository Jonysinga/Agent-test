from __future__ import annotations

import sqlite3
from collections import Counter
from datetime import datetime, timezone

from agentops_assessment.backend import database


def _parse_iso(s: str | None) -> datetime | None:
    """安全解析 ISO 时间字符串，解析失败返回 None。"""
    if not s:
        return None
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return None


def build_dashboard(conn: sqlite3.Connection) -> dict:
    """构建管理后台 Dashboard 指标。

    保留既有字段（名称不变），追加：
      average_run_seconds  — 已完成 run 的平均耗时（秒，保留 2 位小数）
      recent_failures      — 最近 5 条 failed run（error 脱敏）
      queue_backlog        — 当前 queued/running 数量（加分项）
      permission_denied_count — audit_logs 中 deny 总数（加分项）
    """
    from agentops_assessment.security.redaction import redact

    # 既有字段
    task_count = conn.execute("SELECT COUNT(*) AS c FROM tasks").fetchone()["c"]
    run_count = conn.execute("SELECT COUNT(*) AS c FROM runs").fetchone()["c"]
    failed_count = conn.execute(
        "SELECT COUNT(*) AS c FROM runs WHERE status = 'failed'"
    ).fetchone()["c"]
    completed_count = conn.execute(
        "SELECT COUNT(*) AS c FROM runs WHERE status = 'completed'"
    ).fetchone()["c"]
    token_cost = conn.execute(
        "SELECT COALESCE(SUM(token_cost), 0) AS c FROM runs"
    ).fetchone()["c"]
    events = conn.execute(
        "SELECT tool_name FROM run_events WHERE tool_name IS NOT NULL"
    ).fetchall()
    tool_call_counts = dict(Counter(row["tool_name"] for row in events))

    # average_run_seconds：仅计算已有 started_at 和 finished_at 的 run
    finished_rows = conn.execute(
        "SELECT started_at, finished_at FROM runs WHERE finished_at IS NOT NULL AND started_at IS NOT NULL"
    ).fetchall()
    durations: list[float] = []
    for row in finished_rows:
        s = _parse_iso(row["started_at"])
        e = _parse_iso(row["finished_at"])
        if s and e:
            durations.append((e - s).total_seconds())
    average_run_seconds = round(sum(durations) / len(durations), 2) if durations else 0

    # recent_failures：最近 5 条，error 脱敏
    failure_rows = conn.execute(
        "SELECT id, error, finished_at FROM runs WHERE status='failed' ORDER BY finished_at DESC LIMIT 5"
    ).fetchall()
    recent_failures = [
        {
            "run_id": row["id"],
            "error": redact(row["error"] or ""),
            "finished_at": row["finished_at"],
        }
        for row in failure_rows
    ]

    # queue_backlog（加分项）
    queue_backlog = conn.execute(
        "SELECT COUNT(*) AS c FROM runs WHERE status IN ('queued','running')"
    ).fetchone()["c"]

    # permission_denied_count（加分项）
    permission_denied_count = conn.execute(
        "SELECT COUNT(*) AS c FROM audit_logs WHERE decision='deny'"
    ).fetchone()["c"]

    return {
        # 既有字段（名称保持稳定）
        "task_count": task_count,
        "run_count": run_count,
        "completed_count": completed_count,
        "failed_count": failed_count,
        "failure_rate": failed_count / run_count if run_count else 0,
        "token_cost": token_cost,
        "tool_call_counts": tool_call_counts,
        "generated_at": database.now_iso(),
        # 新增字段
        "average_run_seconds": average_run_seconds,
        "recent_failures": recent_failures,
        "queue_backlog": queue_backlog,
        "permission_denied_count": permission_denied_count,
    }
