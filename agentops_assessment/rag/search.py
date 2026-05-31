from __future__ import annotations

import math
import re
from collections import Counter
from typing import Any

from agentops_assessment.backend import database


def tokenize(text: str) -> list[str]:
    return re.findall(r"[A-Za-z0-9-]+|[一-鿿]", text.lower())


def cosine_score(query_tokens: list[str], doc_tokens: list[str]) -> float:
    if not query_tokens or not doc_tokens:
        return 0.0
    q = Counter(query_tokens)
    d = Counter(doc_tokens)
    dot = sum(q[token] * d[token] for token in q.keys() & d.keys())
    q_norm = math.sqrt(sum(v * v for v in q.values()))
    d_norm = math.sqrt(sum(v * v for v in d.values()))
    if not q_norm or not d_norm:
        return 0.0
    return dot / (q_norm * d_norm)


# 提示词注入特征：文档正文中含有这类指令时，过滤对应行，不回显给用户
_INJECTION_LINE_RE = re.compile(
    r"忽略.*(之前|上面).*指令|泄露.*机密|ignore.*previous.*instruction",
    re.IGNORECASE,
)


class KnowledgeIndex:
    """权限感知的本地知识检索索引。

    - 只向用户返回其有权限访问的 chunk（基于 permission 字段）。
    - 按 cosine 相似度重排，取 top_k。
    - 答案生成：拼接 chunk 摘要，过滤含提示词注入特征的行。
    - 返回 citations（doc_id/title/source_path/chunk_id）和 filtered_doc_ids。
    - 不向 API 返回 debug / candidate_note 字段。
    """

    def search(
        self,
        query: str,
        user_permissions: list[str],
        top_k: int = 3,
    ) -> dict[str, Any]:
        with database.connect() as conn:
            database.init_db(conn)
            rows = conn.execute(
                "SELECT id, doc_id, source_path, title, permission, content FROM knowledge_chunks"
            ).fetchall()

        # 1. 权限过滤
        # knowledge:read 是公开权限；其他 permission 值需要用户显式拥有该权限。
        visible: list = []
        filtered_doc_ids: set[str] = set()
        for row in rows:
            perm = row["permission"]
            if perm == "knowledge:read" or perm in user_permissions:
                visible.append(row)
            else:
                filtered_doc_ids.add(row["doc_id"])

        # 2. 余弦相似度重排
        query_tokens = tokenize(query)
        scored = sorted(
            (
                (cosine_score(query_tokens, tokenize(row["content"])), row)
                for row in visible
            ),
            key=lambda x: x[0],
            reverse=True,
        )
        top_chunks = [row for _, row in scored[:top_k]]

        # 3. 答案生成：逐行过滤注入文本，取前 200 字符拼接摘要
        safe_snippets: list[str] = []
        for row in top_chunks:
            lines = [
                line
                for line in row["content"].splitlines()
                if not _INJECTION_LINE_RE.search(line)
            ]
            snippet = " ".join(lines)[:200].strip()
            if snippet:
                safe_snippets.append(snippet)

        answer = "根据知识库：" + "；".join(safe_snippets) if safe_snippets else "未检索到相关规则。"

        # 4. citations（四字段：doc_id / title / source_path / chunk_id）
        citations = [
            {
                "doc_id": row["doc_id"],
                "title": row["title"],
                "source_path": row["source_path"],
                "chunk_id": row["id"],
            }
            for row in top_chunks
        ]

        # 5. 返回（无 debug / candidate_note）
        return {
            "answer": answer,
            "citations": citations,
            "filtered_doc_ids": sorted(filtered_doc_ids),
        }
