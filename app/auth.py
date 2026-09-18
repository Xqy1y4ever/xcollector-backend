"""鉴权：令牌 → 身份。

**两个范围**：

    API_TOKEN    服务令牌 —— 只有 bot 有。能跨用户读写所有人的数据。
                 所以它**绝不外传**，也不进浏览器。
    UserToken    用户令牌 —— 每个用户注册时签发（见 users.py），绑定到具体的
                 user_id，只能碰自己的数据。前端登录页输的就是它。

用户端点的归属判定规则（`resolve_owner`）：

    UserToken            → 归属就是它绑定的那个用户，**无视请求里的 user_id**
                           （否则改个 query 参数就能读别人）
    服务令牌 + user_id   → 用显式指定的用户（bot 发每日摘要时要按用户读）

**绝不写「传了 `?user_id=` 就从用户令牌切走」** —— 那正是越权最常见的写法。
服务令牌必须显式传；用户令牌传了也无效。

范围不够返回 **403**（身份有效但没权限），令牌不对返回 **401**。前端据此区分
"去重新登录"和"你没这个权限"。
"""

from __future__ import annotations

import logging
import secrets
from dataclasses import dataclass
from enum import Enum
from typing import Annotated

from fastapi import Depends, Header, HTTPException

from .config import get_settings
from .users import get_user_by_token, touch_user

logger = logging.getLogger(__name__)


class Scope(str, Enum):
    SERVICE = "service"
    USER = "user"


@dataclass(frozen=True)
class Identity:
    """一次请求的调用者。"""

    scope: Scope
    user_id: str | None = None
    token: str = ""

    @property
    def is_service(self) -> bool:
        return self.scope is Scope.SERVICE

    @property
    def is_user(self) -> bool:
        return self.scope is Scope.USER


SERVICE = Identity(scope=Scope.SERVICE)


def _parse_bearer(authorization: str | None) -> str:
    scheme, _, token = (authorization or "").partition(" ")
    if scheme.lower() != "bearer":
        return ""
    return token.strip()


async def require_token(
    authorization: Annotated[str | None, Header()] = None,
) -> Identity:
    """所有 /api 接口的入口鉴权。

    未配 `API_TOKEN` = 本地开发模式，**不校验**且按服务令牌放行（启动时打过
    WARNING）。"忘了配"和"故意不配"在日志里必须能区分开。
    """
    settings = get_settings()
    if not settings.auth_enabled:
        return SERVICE

    token = _parse_bearer(authorization)
    if not token:
        raise HTTPException(
            status_code=401,
            detail="缺少 Authorization: Bearer <令牌>",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # 服务令牌先比：定长比较，且不走用户表的查询路径
    if secrets.compare_digest(token, settings.api_token.strip()):
        return Identity(scope=Scope.SERVICE, token=token)

    row = await get_user_by_token(token)
    if row is None:
        raise HTTPException(
            status_code=401,
            detail="令牌无效",
            headers={"WWW-Authenticate": "Bearer"},
        )
    await touch_user(str(row["id"]))
    return Identity(scope=Scope.USER, user_id=str(row["id"]), token=token)


async def optional_token(
    authorization: Annotated[str | None, Header()] = None,
) -> Identity | None:
    """像 `require_token`，但没带令牌时**返回 None 而不是抛**。

    只给附件下载用：那条路允许"没有 Authorization 头"，改由签名来判断。
    """
    settings = get_settings()
    if not settings.auth_enabled:
        return SERVICE
    if not _parse_bearer(authorization):
        return None
    try:
        return await require_token(authorization)
    except HTTPException:
        return None


async def require_service(
    identity: Annotated[Identity, Depends(require_token)],
) -> Identity:
    """只允许服务令牌（bot）调用的接口。

    挂在这些上面：入库（messages / notifications / attachments / groups /
    gap-alerts / stats / digest-log / state）、签发验证码、发邀请码。

    **刻意不挂**在 `corrections` 和 `read` 上 —— 那是用户对自己条目的标注。
    """
    if not identity.is_service:
        raise HTTPException(
            status_code=403,
            detail="该接口只允许服务端（bot）调用。用户令牌只能读写自己的数据。",
        )
    return identity


async def require_user(
    identity: Annotated[Identity, Depends(require_token)],
) -> Identity:
    """只允许用户令牌调用的接口（自己的订阅、自己的资料）。"""
    if not identity.is_user:
        raise HTTPException(
            status_code=403,
            detail="该接口需要用户令牌（在登录页输入的那个）。",
        )
    return identity


def resolve_owner(identity: Identity, requested: str | None) -> str:
    """定出这次请求动的是**谁的数据**。

    用户令牌 → 永远是自己（requested 被忽略，这正是关键）；
    服务令牌 → 必须显式给 user_id：调用方没说清要动谁，就报错，
               猜一个的后果比报错严重得多。
    """
    if identity.is_user:
        if not identity.user_id:  # pragma: no cover - 构造上不可能
            raise HTTPException(status_code=401, detail="令牌无效")
        return identity.user_id

    owner = (requested or "").strip()
    if not owner:
        raise HTTPException(
            status_code=400,
            detail="服务令牌必须显式指定 user_id：这次请求要动谁的数据？",
        )
    return owner
