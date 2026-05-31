from __future__ import annotations

import re

PROMPT_INJECTION_PATTERNS = [
    re.compile(r"ignore\s+(all\s+)?previous\s+instructions", re.IGNORECASE),
    re.compile(r"reveal\s+(all\s+)?(secrets|credentials|tokens)", re.IGNORECASE),
    re.compile(r"export\s+.*(secret|credential|token)", re.IGNORECASE),
    re.compile(r"忽略.*(之前|上面).*指令"),
    re.compile(r"泄露.*(机密|凭证|token|令牌)"),
    re.compile(r"绕过.*权限"),
]


def detect_prompt_injection(text: str) -> list[str]:
    """返回命中的提示词注入模式。调用方在任务创建和 RAG 输出路径接线。"""
    return [pattern.pattern for pattern in PROMPT_INJECTION_PATTERNS if pattern.search(text)]
