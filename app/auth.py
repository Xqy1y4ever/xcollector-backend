"""鉴权：令牌 → 范围（scope）。

**为什么分范围**：整套系统原来只有一个共享密钥，而它必须交给登录页 ——
于是任何能打开网页、看一眼浏览器存储的人都能调**所有**写接口：伪造入库、
改机器字段、删通知。但网页实际上只需要"看 + 人工修正 + 标已读"。

所以拆成两个令牌：

    API_TOKEN       写入范围（write）—— 只有 bot 知道，**永远不进浏览器**
    WEB_API_TOKEN   网页范围（web）  —— 登录页用，只读 + 那两个标注接口

`WEB_API_TOKEN` 留空 = 退回单令牌模式（`API_TOKEN` 同时充当网页令牌），
已有部署升级上来行为完全不变。

范围不够时返回 **403 而不是 401**：调用方身份是有效的，只是没这个权限 ——
前端据此能区分"令牌错了，去重新登录"和"你没这个权限"。
"""

from __future__ import annotations

import logging
import secrets
from enum import Enum
from typing import Annotated

from fastapi import Depends, Header, HTTPException

from .config import get_settings

logger = logging.getLogger(__name__)

# 只在启动时提醒一次"两个令牌配成一样了"，不要每个请求都刷
_warned_same_token = False


class Scope(str, Enum):
    WRITE = "write"
    WEB = "web"


def resolve_scope(token: str) -> Scope | None:
    """把令牌换成范围；不认识就返回 None。

    两个令牌都**无条件比对**（不用 if/elif 短路），比对本身是定长的，
    这样"令牌对不对"不会从响应时间上泄漏出去。
    """
    prepared = (token or "").strip()
    if not prepared:
        return None

    settings = get_settings()
    write_token = settings.api_token.strip()
    # 网页令牌没配 = 单令牌模式：API_TOKEN 同时当网页令牌用
    web_token = settings.web_api_token.strip() or write_token

    if not write_token and not web_token:
        return None

    is_write = bool(write_token) and secrets.compare_digest(prepared, write_token)
    is_web = bool(web_token) and secrets.compare_digest(prepared, web_token)

    if is_write:
        return Scope.WRITE
    if is_web:
        return Scope.WEB
    return None


def _parse_bearer(authorization: str | None) -> str:
    scheme, _, token = (authorization or "").partition(" ")
    if scheme.lower() != "bearer":
        return ""
    return token.strip()


async def require_token(
    authorization: Annotated[str | None, Header()] = None,
) -> Scope:
    """所有 /api 接口的入口鉴权。返回调用方的范围。

    未配 `API_TOKEN` = 本地开发模式，**不校验**且按写入范围放行（启动时打过
    WARNING）。"忘了配"和"故意不配"在日志里必须能区分开。
    """
    global _warned_same_token

    settings = get_settings()
    if not settings.auth_enabled:
        return Scope.WRITE

    if (
        not _warned_same_token
        and settings.web_api_token.strip()
        and settings.web_api_token.strip() == settings.api_token.strip()
    ):
        _warned_same_token = True
        logger.warning(
            "WEB_API_TOKEN 与 API_TOKEN 相同：网页令牌因此拥有完整写入权限，"
            "这次拆分等于没做。请给网页端换一个不同的令牌。"
        )

    token = _parse_bearer(authorization)
    if not token:
        raise HTTPException(
            status_code=401,
            detail="缺少 Authorization: Bearer <令牌>",
            headers={"WWW-Authenticate": "Bearer"},
        )

    scope = resolve_scope(token)
    if scope is None:
        raise HTTPException(
            status_code=401,
            detail="令牌无效",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return scope


async def optional_token(
    authorization: Annotated[str | None, Header()] = None,
) -> Scope | None:
    """像 `require_token`，但没带令牌时**返回 None 而不是抛**。

    只给附件下载用：那条路允许"没有 Authorization 头"，改由签名来判断。
    """
    settings = get_settings()
    if not settings.auth_enabled:
        return Scope.WRITE
    token = _parse_bearer(authorization)
    if not token:
        return None
    return resolve_scope(token)


async def require_write(
    scope: Annotated[Scope, Depends(require_token)],
) -> Scope:
    """写接口的门槛：只有写入令牌能过。

    挂在这些接口上：入库（messages / notifications / attachments / groups /
    gap-alerts / stats / digest-log / state）。

    **刻意不挂**在 `POST /notifications/{id}/corrections` 和
    `POST /notifications/{id}/read` 上 —— 那两个是人工标注，网页必须能调，
    而且 corrections 只追加、不覆盖机器字段。
    """
    if scope is not Scope.WRITE:
        raise HTTPException(
            status_code=403,
            detail=(
                "该接口只允许 bot 调用（需要写入令牌 API_TOKEN）。"
                "网页令牌只能读取、提交人工修正、标记已读。"
            ),
        )
    return scope
