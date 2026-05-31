from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Header, HTTPException, Request, status

from agentops_assessment.backend import database


def _decode_user(row) -> dict:
    return {
        "id": row["id"],
        "name": row["name"],
        "roles": database.decode_json(row["roles_json"], []),
        "permissions": database.decode_json(row["permissions_json"], []),
    }


def get_user(user_id: str) -> dict | None:
    with database.connect() as conn:
        database.init_db(conn)
        row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        return _decode_user(row) if row else None


def get_current_user(x_user_id: Annotated[str | None, Header()] = None) -> dict:
    if not x_user_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="缺少 X-User-Id 请求头。",
        )
    user = get_user(x_user_id)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"未知用户: {x_user_id}",
        )
    return user


def require_permissions(*permissions: str):
    """权限检查依赖。缺少权限时写 deny 审计日志后返回 403。

    审计 payload 含 missing_permissions 和 required，方便管理员追溯。
    """
    def dependency(
        request: Request,
        user: dict = Depends(get_current_user),
    ) -> dict:
        missing = [p for p in permissions if p not in user["permissions"]]
        if missing:
            resource_hint = f"{request.method} {request.url.path}"
            with database.connect() as conn:
                database.init_db(conn)
                database.insert_audit_log(
                    conn,
                    actor_id=user["id"],
                    action="permission.denied",
                    resource=resource_hint,
                    payload={
                        "missing_permissions": missing,
                        "required": list(permissions),
                    },
                    decision="deny",
                )
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={"missing_permissions": missing},
            )
        return user

    return dependency
