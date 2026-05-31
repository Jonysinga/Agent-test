from __future__ import annotations

from collections.abc import Callable
from typing import Any

from agentops_assessment.agent.planner import PlanStep
from agentops_assessment.agent.state import InMemoryRunStateStore, RunState, StepState
from agentops_assessment.agent.tools import ToolRegistry

# 工具权限矩阵：tool_name → (required_permission, skippable)
# skippable=True 表示缺少权限时跳过（tool.skipped）而非令 run failed。
# OA 写操作标记 skippable=True，缺权限时静默跳过并写 deny audit。
TOOL_PERMISSIONS: dict[str, tuple[str, bool]] = {
    "erp.get_inventory":        ("erp:read",          False),
    "bi.get_sales":             ("bi:read",            False),
    "knowledge.search":         ("knowledge:read",     False),
    "supplier.get_risk":        ("supplier:read",      False),
    "oa.create_approval_draft": ("oa:approval:write",  True),
}

# FakeLLM 每次调用固定 completion_tokens
_FAKE_LLM_TOKENS_PER_STEP = 24


def _summarize(obj: dict, max_val_len: int = 120) -> dict:
    """截断 dict 中过长的字符串 value，避免事件 payload 膨胀。"""
    result: dict = {}
    for k, v in obj.items():
        if isinstance(v, str) and len(v) > max_val_len:
            result[k] = v[:max_val_len] + "..."
        elif isinstance(v, (dict, list)):
            result[k] = f"<{type(v).__name__} len={len(v)}>"
        else:
            result[k] = v
    return result


def _render_args(template: dict, context: dict, step_outputs: dict) -> dict:
    """把 input_template 中 "$key" 占位替换为真实值。

    合并顺序：context（sku/intent/user_permissions 等）< 各步骤输出展平值，
    后者覆盖前者（ERP 输出的 supplier_id/stock_gap 等在此注入）。
    找不到对应 key 时保留原始占位串，Executor 会注意到但不崩溃。
    """
    merged: dict = {**context}
    for output in step_outputs.values():
        if isinstance(output, dict):
            merged.update(output)

    result: dict = {}
    for k, v in template.items():
        if isinstance(v, str) and v.startswith("$"):
            key = v[1:]
            result[k] = merged.get(key, v)
        else:
            result[k] = v
    return result


def _should_create_oa_draft(
    intent: str,
    erp_output: dict,
    bi_output: dict,
    oa_rules: dict,
) -> bool:
    """判断业务上是否需要创建 OA 草稿（不含权限判断，权限由执行期闸门处理）。

    条件：
    1. 意图不是 analysis_only。
    2. 存在库存缺口或预测超现库存（有补货必要性）。
    3. 缺口 >= auto_draft_threshold_units 或预测销售额 >= manual_review_threshold_usd（达阈值）。
    """
    if intent == "analysis_only":
        return False
    rules = oa_rules.get("inventory_replenishment", {})
    gap = erp_output.get("stock_gap", 0)
    current_stock = erp_output.get("current_stock", 0)
    forecast_units = bi_output.get("forecast_units_next_14d", 0)
    forecast_sales = bi_output.get("sales_usd_14d", 0)  # 来自 BI，不在 ERP 里

    replenishment_needed = gap > 0 or forecast_units > current_stock
    threshold_hit = (
        gap >= rules.get("auto_draft_threshold_units", 30)
        or forecast_sales >= rules.get("manual_review_threshold_usd", 5000)
    )
    return replenishment_needed and threshold_hit


def _build_result(context: dict, step_outputs: dict, plan: list[PlanStep]) -> dict:
    """从各步骤输出构造最终业务摘要，不返回原始工具输出。

    recommended_action 三种标准值：
      "create_replenishment_approval"               — 实际创建了 OA 草稿
      "replenishment_recommended_no_write_permission" — 需要补货但无 OA 写权限
      "analysis_complete"                           — 纯分析任务或未达补货阈值
    """
    from agentops_assessment.security.redaction import redact

    erp = step_outputs.get("erp", {})
    bi = step_outputs.get("bi", {})
    sup = step_outputs.get("supplier", {})
    rag = step_outputs.get("knowledge", {})
    oa = step_outputs.get("oa", {})

    has_draft = bool(oa.get("approval_draft_id"))

    if has_draft:
        action = "create_replenishment_approval"
    elif context.get("oa_business_required") and context.get("oa_permission_denied"):
        action = "replenishment_recommended_no_write_permission"
    else:
        action = "analysis_complete"

    result: dict = {
        "sku": context.get("sku"),
        "warehouse": erp.get("warehouse"),
        "stock_gap": erp.get("stock_gap"),
        "forecast_units_next_14d": bi.get("forecast_units_next_14d"),
        "supplier_risk": {
            "supplier_id": sup.get("supplier_id"),
            "risk_level": sup.get("risk_level"),
        },
        "citations": rag.get("citations", []),
        "recommended_action": action,
    }
    if has_draft:
        result["approval_draft_id"] = oa["approval_draft_id"]

    return redact(result)  # 最终出口脱敏兜底


class Executor:
    def __init__(
        self,
        registry: ToolRegistry,
        state_store: InMemoryRunStateStore | None = None,
    ) -> None:
        self.registry = registry
        self.state_store = state_store or InMemoryRunStateStore()

    def execute(
        self,
        run_id: str,
        plan: list[PlanStep],
        context: dict[str, Any],
        event_writer: Callable | None = None,
        audit_writer: Callable | None = None,
    ) -> RunState:
        """按计划执行工具链，渲染入参，持久化事件，汇总结果。

        - event_writer(run_id, event_type, tool_name, payload): 写 run_events。
        - audit_writer(actor_id, action, resource, decision, payload): 写 audit_logs。
        - 两个回调内部各自建立独立 DB 连接，避免跨线程共享 conn。
        - 保证无论如何都落终态（completed 或 failed），不留 running 僵尸。
        """
        from agentops_assessment.security.redaction import redact

        # 空回调兜底（单测可以传 None）
        _event = event_writer or (lambda *_: None)
        _audit = audit_writer or (lambda *_: None)

        state = RunState(
            run_id=run_id,
            status="running",
            steps=[StepState(step_id=s.id, tool_name=s.tool_name) for s in plan],
        )
        self.state_store.save(state)

        step_outputs: dict[str, dict] = {}
        user_permissions: list[str] = context.get("user_permissions", [])
        intent: str = context.get("intent", "analysis_only")
        oa_rules: dict = context.get("oa_rules", {})
        token_cost = 0

        for i, step in enumerate(plan):
            step_state = state.steps[i]

            # ── 特殊 tool: error.no_sku ──────────────────────────────────────
            if step.tool_name == "error.no_sku":
                step_state.status = "failed"
                step_state.error = "无法从任务 prompt 中识别 SKU，执行终止。"
                state.status = "failed"
                state.result = {"error": step_state.error}
                self.state_store.save(state)
                return state

            # ── OA 步骤：业务阈值裁决（先于权限闸门，避免无谓审计）──────────
            if step.tool_name == "oa.create_approval_draft":
                erp_out = step_outputs.get("erp", {})
                bi_out = step_outputs.get("bi", {})
                oa_needed = _should_create_oa_draft(intent, erp_out, bi_out, oa_rules)
                context["oa_business_required"] = oa_needed
                if not oa_needed:
                    step_state.status = "skipped"
                    _event(run_id, "tool.skipped", step.tool_name,
                           {"reason": "business_rule: analysis_only or below threshold"})
                    continue

            # ── 权限闸门 ─────────────────────────────────────────────────────
            required_perm, skippable = TOOL_PERMISSIONS.get(step.tool_name, ("", False))
            if required_perm and required_perm not in user_permissions:
                _audit(
                    context.get("user_id", "unknown"),
                    "tool.call",
                    step.tool_name,
                    "deny",
                    {"missing_permission": required_perm},
                )
                if skippable:
                    context["oa_permission_denied"] = True
                    step_state.status = "skipped"
                    _event(run_id, "tool.skipped", step.tool_name,
                           {"reason": f"permission denied: {required_perm}"})
                    continue
                else:
                    step_state.status = "failed"
                    step_state.error = f"缺少权限 {required_perm}，无法执行 {step.tool_name}。"
                    state.status = "failed"
                    state.result = {"error": step_state.error}
                    self.state_store.save(state)
                    return state

            # ── 渲染入参 ─────────────────────────────────────────────────────
            args = _render_args(step.input_template, context, step_outputs)

            # ── 调用工具（内置 transient 重试）────────────────────────────────
            try:
                raw_output = self.registry.call(step.tool_name, args)
                attempts = self.registry.last_call_attempts.get(step.tool_name, 1)
                output = redact(raw_output)

                step_state.status = "success"
                step_state.output = output
                step_outputs[step.id] = output

                event_payload: dict = {
                    "args_summary": redact(_summarize(args)),
                    "output_summary": _summarize(output),
                }
                if attempts > 1:
                    event_payload["retry_attempts"] = attempts
                _event(run_id, "tool.call", step.tool_name, event_payload)
                _audit(
                    context.get("user_id", "unknown"),
                    "tool.call",
                    step.tool_name,
                    "allow",
                    {"tool": step.tool_name},
                )

                # OA 草稿成功创建时写专用审计
                if step.tool_name == "oa.create_approval_draft":
                    _audit(
                        context.get("user_id", "unknown"),
                        "approval.draft.create",
                        output.get("approval_draft_id", ""),
                        "allow",
                        {"sku": context.get("sku"), "approval_type": "inventory_replenishment"},
                    )

                token_cost += _FAKE_LLM_TOKENS_PER_STEP

            except Exception as exc:
                step_state.status = "failed"
                step_state.error = redact(str(exc))  # 不泄露原始堆栈
                state.status = "failed"
                state.result = {"error": step_state.error}
                self.state_store.save(state)
                return state

        # ── 汇总业务结果 ──────────────────────────────────────────────────────
        state.status = "completed"
        state.result = _build_result(context, step_outputs, plan)
        state.token_cost = token_cost
        self.state_store.save(state)
        return state
