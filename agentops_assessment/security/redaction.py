from __future__ import annotations

import re
from typing import Any

# 敏感键名集合（小写比较）。命中时删除整项，key 本身也不能出现在 API 响应/事件/审计里。
_SENSITIVE_KEYS = frozenset({
    "vendor_secret",
    "unit_cost_usd",
    "debug",
    "candidate_note",
    "password",
    "token",
    "secret",
    "credential",
})

# 值级别黑名单：已知泄漏字符串 + 异常堆栈
_SENSITIVE_VALUE_PATTERNS = [
    re.compile(r"ACME-TIER-\d+-REBATE"),
    re.compile(r"[A-Z]+-PRICE-FLOOR"),
    re.compile(r"Traceback \(most recent call last\)"),
]

_REDACTED = "[REDACTED]"


def redact(obj: Any) -> Any:
    """递归脱敏 dict/list/str。

    - 敏感 dict key 直接删除（key 名本身也不得出现在输出中）
    - 敏感字符串值替换为 [REDACTED]
    - list/tuple 递归处理每个元素
    - 其余类型原样返回
    """
    if isinstance(obj, dict):
        return {
            k: redact(v)
            for k, v in obj.items()
            if not _is_sensitive_key(k)
        }
    if isinstance(obj, (list, tuple)):
        return [redact(item) for item in obj]
    if isinstance(obj, str):
        return _REDACTED if _is_sensitive_value(obj) else obj
    return obj


def _is_sensitive_key(key: str) -> bool:
    """判断 dict key 是否敏感（小写匹配）。"""
    normalized = key.lower()
    if normalized in _SENSITIVE_KEYS:
        return True
    # 包含 secret / credential 关键词的 key 一律删除（如 api_secret、auth_credential）。
    # token 仅删除凭证型字段，保留 token_cost / prompt_tokens 这类公开指标。
    if any(marker in normalized for marker in ("secret", "credential")):
        return True
    return normalized.endswith("_token") or normalized in {"access_token", "refresh_token"}


def _is_sensitive_value(value: str) -> bool:
    """判断字符串值是否命中已知敏感模式。"""
    return any(p.search(value) for p in _SENSITIVE_VALUE_PATTERNS)
