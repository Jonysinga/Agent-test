from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from agentops_assessment.agent.fake_llm import FakeLLM

# SKU 格式：2+ 大写字母开头，后跟连字符分隔的字母数字段，如 SKU-001、PROD-A1-B2
_SKU_RE = re.compile(r"\b([A-Z]{2,}-[A-Z0-9]+(?:-[A-Z0-9]+)*)\b")

# 意图识别关键词（优先级：analysis_only > approval_draft_requested > approval_recommendation > 默认 analysis_only）
_ANALYSIS_ONLY = re.compile(
    r"只分析|仅分析|不创建|不要审批|无需审批|analysis[\s_-]*only",
    re.IGNORECASE,
)
_DRAFT_REQUESTED = re.compile(
    r"创建.*审批草稿|生成.*审批草稿|创建\s*OA|发起审批|create.*approval.*draft",
    re.IGNORECASE,
)
_RECOMMENDATION = re.compile(
    r"补货审批建议|审批建议|补货建议|replenishment.*suggest",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class PlanStep:
    id: str
    tool_name: str
    description: str
    input_template: dict[str, Any] = field(default_factory=dict)


def _extract_sku(prompt: str) -> str | None:
    """从 prompt 中提取第一个 SKU 形态 token。不写死具体 SKU 值。"""
    m = _SKU_RE.search(prompt)
    return m.group(1) if m else None


def _detect_intent(prompt: str) -> str:
    """三层意图识别（确定性关键词规则）。

    返回值：
      "analysis_only"            — 明确只读，不加 OA 步骤
      "approval_draft_requested" — 明确要创建草稿，加 OA 候选步骤
      "approval_recommendation"  — 建议类，Executor 结合阈值+权限裁决是否真正创建草稿
    默认返回 analysis_only，防止分析任务产生写副作用。
    """
    if _ANALYSIS_ONLY.search(prompt):
        return "analysis_only"
    if _DRAFT_REQUESTED.search(prompt):
        return "approval_draft_requested"
    if _RECOMMENDATION.search(prompt):
        return "approval_recommendation"
    return "analysis_only"


class Planner:
    def __init__(self, llm: FakeLLM | None = None) -> None:
        self.llm = llm or FakeLLM()

    def create_plan(self, prompt: str, context: dict[str, Any] | None = None) -> list[PlanStep]:
        """从 prompt 推断 SKU 和意图，生成确定性工具链计划。

        - 提取 SKU（正则），提取不到则返回单步 error.no_sku 计划，
          Executor 见到该 tool_name 直接令 run 进入可解释 failed。
        - 识别意图（三层关键词规则）。
        - 标准链路：ERP → BI → Knowledge → Supplier，必要时追加 OA 步骤。
        - 把 sku 和 intent 写入 context（引用传递），供 Executor 直接使用。
        """
        ctx = context if context is not None else {}

        # 用 FakeLLM 估算 token（结果不使用，保持接口一致）
        self.llm.complete(prompt)

        sku = _extract_sku(prompt)
        if not sku:
            return [
                PlanStep(
                    id="no_sku",
                    tool_name="error.no_sku",
                    description="无法从 prompt 中提取 SKU，无法继续执行。",
                )
            ]

        intent = _detect_intent(prompt)
        ctx["sku"] = sku
        ctx["intent"] = intent

        steps: list[PlanStep] = [
            PlanStep(
                id="erp",
                tool_name="erp.get_inventory",
                description="查询 ERP 库存状态",
                input_template={"sku": "$sku"},
            ),
            PlanStep(
                id="bi",
                tool_name="bi.get_sales",
                description="查询 BI 销售预测",
                input_template={"sku": "$sku"},
            ),
            PlanStep(
                id="knowledge",
                tool_name="knowledge.search",
                description="检索知识库：库存处理与补货审批规则",
                input_template={
                    "query": "库存异常补货审批规则",
                    "user_permissions": "$user_permissions",  # Executor 运行期注入
                    "top_k": 3,
                },
            ),
            PlanStep(
                id="supplier",
                tool_name="supplier.get_risk",
                description="查询供应商风险评级",
                input_template={"supplier_id": "$supplier_id"},  # 来自 ERP 输出
            ),
        ]

        # approval_recommendation 和 approval_draft_requested 均加 OA 候选步骤，
        # Executor 执行期结合业务阈值和权限做最终裁决。
        if intent in ("approval_draft_requested", "approval_recommendation"):
            steps.append(
                PlanStep(
                    id="oa",
                    tool_name="oa.create_approval_draft",
                    description="创建 OA 补货审批草稿",
                    input_template={
                        "sku": "$sku",
                        "stock_gap": "$stock_gap",
                        "approval_type": "inventory_replenishment",
                    },
                )
            )

        return steps
