from __future__ import annotations

import json
import os
from pathlib import Path

from agentops_assessment.backend import database


def execute_run(run_id: str) -> None:
    """后台执行入口：串联 Planner → Executor，保证 run 永远落终态。

    流程：
    1. 读取 run / task / user 记录，置 running 状态。
    2. 加载 oa_rules.json（注入 Executor context，避免重复读文件）。
    3. 构造 event_writer / audit_writer 回调——各自独立 connect，避免跨线程共享 conn。
    4. 调用 Planner.create_plan + Executor.execute。
    5. 根据 RunState 落库 completed 或 failed（含 token_cost 和 result_json）。
    6. 兜底 except：任何未捕获异常都落 failed，不留 running 僵尸。
    """
    from agentops_assessment.agent.executor import Executor
    from agentops_assessment.agent.planner import Planner
    from agentops_assessment.agent.tools import ToolRegistry
    from agentops_assessment.security.redaction import redact

    # ── 1. 读 run + task + user，置 running ──────────────────────────────────
    with database.connect() as conn:
        database.init_db(conn)
        run_row = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        if not run_row:
            return
        run = dict(run_row)
        task_row = conn.execute(
            "SELECT * FROM tasks WHERE id = ?", (run["task_id"],)
        ).fetchone()
        if not task_row:
            return
        task = dict(task_row)
        user_row = conn.execute(
            "SELECT * FROM users WHERE id = ?", (run["requested_by"],)
        ).fetchone()
        user = {
            "id": user_row["id"],
            "permissions": database.decode_json(user_row["permissions_json"], []),
        }
        now = database.now_iso()
        conn.execute(
            "UPDATE runs SET status=?, started_at=? WHERE id=?",
            ("running", now, run_id),
        )
        conn.commit()

    # ── 2. 加载 OA 规则 ───────────────────────────────────────────────────────
    fixtures_dir = Path(os.getenv("ASSESSMENT_FIXTURES_DIR", "fixtures"))
    oa_rules_path = fixtures_dir / "business" / "oa_rules.json"
    try:
        oa_rules = json.loads(oa_rules_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        oa_rules = {}

    # ── 3. 构造回调（各自独立 connect，不共享 conn）──────────────────────────
    def event_writer(run_id_: str, event_type: str, tool_name: str, payload: dict) -> None:
        with database.connect() as c:
            database.init_db(c)
            database.insert_run_event(c, run_id_, event_type, redact(payload), tool_name)

    def audit_writer(
        actor_id: str, action: str, resource: str, decision: str, payload: dict
    ) -> None:
        with database.connect() as c:
            database.init_db(c)
            database.insert_audit_log(
                c, actor_id, action, resource, redact(payload), decision
            )

    # ── 4. context ────────────────────────────────────────────────────────────
    context: dict = {
        "sku": None,             # Planner.create_plan 写入
        "intent": None,          # Planner.create_plan 写入
        "user_id": user["id"],
        "user_permissions": user["permissions"],
        "oa_rules": oa_rules,
    }

    try:
        registry = ToolRegistry.with_default_clients(fixtures_dir=fixtures_dir)
        plan = Planner().create_plan(task["prompt"], context)
        executor = Executor(registry)
        state = executor.execute(run_id, plan, context, event_writer, audit_writer)

        # ── 5. 落库终态 ───────────────────────────────────────────────────────
        with database.connect() as conn:
            database.init_db(conn)
            if state.status == "completed":
                conn.execute(
                    "UPDATE runs SET status=?, result_json=?, token_cost=?, finished_at=? WHERE id=?",
                    (
                        "completed",
                        database.encode_json(state.result),
                        getattr(state, "token_cost", 0),
                        database.now_iso(),
                        run_id,
                    ),
                )
            else:
                error_msg = ""
                if state.result and isinstance(state.result, dict):
                    error_msg = state.result.get("error", "执行失败")
                error_msg = redact(str(error_msg))
                conn.execute(
                    "UPDATE runs SET status=?, error=?, finished_at=? WHERE id=?",
                    ("failed", error_msg, database.now_iso(), run_id),
                )
            conn.commit()

    except Exception as exc:
        # ── 6. 兜底：任何未捕获异常都落 failed ──────────────────────────────
        with database.connect() as conn:
            database.init_db(conn)
            conn.execute(
                "UPDATE runs SET status=?, error=?, finished_at=? WHERE id=?",
                ("failed", redact(str(exc)), database.now_iso(), run_id),
            )
            conn.commit()
