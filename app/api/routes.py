"""HTTP API —— 纯数据层的增删查改。

契约见 docs/api.md。这个文件里**不应该出现任何业务词**：什么算通知、
该不该处理、DDL 对不对，全部由 xcollector-bot 决定。

唯二的例外是契约自己指定的：
  - `GET /api/notifications` 的**读投影**（实现在 materialize.py）
  - `evidence` 非空的硬约束 —— 后端替 bot 守住"没有证据的条目不许入库"
"""

from __future__ import annotations

import json
import logging
import os
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from .. import __version__
from ..attachments import (
    MultipartError,
    TooLarge,
    content_disposition,
    handle_upload,
    load_bytes,
)
from ..auth import (
    Identity,
    optional_token,
    require_service,
    require_token,
    require_user,
    resolve_owner,
    resolve_owner_optional,
)
from ..config import get_settings
from .. import subscriptions
from ..signing import verify_attachment_sig
from ..db import (
    NOTIFICATION_PATCHABLE,
    RAW_PATCHABLE,
    STAT_FIELDS,
    ack_gap_alert,
    add_correction,
    add_digest_log,
    add_gap_alert,
    add_stats,
    count_attachments,
    count_digest_logs,
    count_messages,
    count_notifications,
    delete_notification,
    delete_state,
    fetch_one,
    get_notification_row,
    get_raw,
    get_stat,
    get_state,
    has_subscription,
    insert_raw_message,
    list_corrections,
    list_digest_logs,
    list_gap_alerts,
    list_groups,
    list_messages,
    list_state,
    patch_notification,
    patch_raw_message,
    put_state,
    set_read,
    upsert_group,
    upsert_notification,
)
from ..materialize import (
    CORRECTABLE_FIELDS,
    count_notification_views,
    get_notification_view,
    list_notification_views,
    raw_view,
    raw_views,
)
from ..users import (
    UserError,
    count_users,
    create_invite,
    get_user_by_id,
    get_user_by_qq,
    issue_verify_code,
    list_invites,
    list_users,
    public_user,
    register_or_rotate,
)
from ..utils import as_int, json_loads, now_ms, preview

logger = logging.getLogger(__name__)

# done = 做完了，archived = 不是通知/误报。这是契约 §2 明确规定的取值集合，
# 属于"接口参数校验"，不是后端在判断业务。
VALID_STATUS = {"active", "archived", "done"}

_TRUE = {"1", "true", "yes", "on", "t"}
_FALSE = {"0", "false", "no", "off", "f", ""}

# --------------------------------------------------------------------------
# 认证：见 auth.py。所有 /api 请求都要带 Authorization: Bearer <令牌>，
# 而**写接口**另外要求那是服务令牌（只有 bot 有）。
#
# 三个 router：
#   router        要 Bearer（默认，绝大多数接口）
#   open_router   不要 Bearer，自己判断"签名 URL 或 Bearer"—— 只放附件下载，
#                 因为浏览器 <img> / <a> 带不了 Authorization 头。
#   public_router 完全不鉴权 —— 只放**注册**。注册的前提就是"还没有令牌"，
#                 所以它天然必须在鉴权之外；安全性由邀请码 + QQ 验证码担着。
# --------------------------------------------------------------------------

router = APIRouter(prefix="/api", dependencies=[Depends(require_token)])
open_router = APIRouter(prefix="/api")
public_router = APIRouter(prefix="/api")

# 写接口统一挂这个：只有服务令牌（bot）能过
WriteDep = Depends(require_service)


# --------------------------------------------------------------------------
# 小工具：查询参数解析
# --------------------------------------------------------------------------


def _flag(value: str | None, *, default: bool = False) -> bool:
    """把 `count_only=1` 这类开关解析成 bool（同时接受 true/yes/on）。"""
    if value is None:
        return default
    text = value.strip().lower()
    if text in _TRUE:
        return True
    if text in _FALSE:
        return False
    raise HTTPException(status_code=400, detail=f"无法识别的布尔值：{value}")


def _optional_flag(value: str | None, field: str) -> bool | None:
    """三态开关：不传 = 不过滤。"""
    if value is None or value.strip() == "":
        return None
    text = value.strip().lower()
    if text in _TRUE:
        return True
    if text in _FALSE:
        return False
    raise HTTPException(status_code=400, detail=f"{field} 只能是 true/false")


def _multi(values: list[str] | None) -> list[str]:
    """`state` 支持重复参数与逗号分隔两种写法（bot 两种都可能用）。"""
    out: list[str] = []
    for raw in values or []:
        for piece in str(raw).split(","):
            piece = piece.strip()
            if piece:
                out.append(piece)
    return out


def _json_text(value: Any, fallback: Any) -> str:
    """把任意 JSON 值编码成库里要存的文本（后端不理解它的含义）。"""
    if value is None:
        value = fallback
    return json.dumps(value, ensure_ascii=False)


# --------------------------------------------------------------------------
# 1. 原始消息 raw_message
# --------------------------------------------------------------------------


# --------------------------------------------------------------------------
# 入库方（bot 或每个人的客户端）
#
# 多用户之前只有 bot 一个写入方，所以"写"就等于"服务令牌"。加了
# xcollector-client 之后，**每个用户也可以在自己的机器上跑一个入库客户端**，
# 用他自己的 UserToken 上报他读到的聊天记录。于是权限模型改成：
#
#   服务令牌（bot）    —— 代表整套部署，写什么都不用再证明什么
#   用户令牌（客户端）  ——
#       写**按用户的那一层**（通知 / 统计 / 缺口 / 自己的键值）：
#           归属被强制成他自己（resolve_owner），碰不到别人，所以直接允许。
#       写**共享层**（raw_message / group_state）：
#           必须证明「这个来源是我自己订阅的」。共享层没有归属，一个人往里写
#           就等于写进所有人看到的那张表 —— 要求先有订阅，既是权限检查，
#           也正好就是产品规则本身（订阅定义"抽什么"）。
#
# 共享层的**读**保持服务令牌专属：那里面有所有人订阅的所有群的消息，
# 让任何一个用户读到就是跨群泄露。写和读在这里是不对称的，是有意的。
# --------------------------------------------------------------------------


async def _require_subscribed(
    identity: Identity, group_id: str, sender_id: str | None
) -> None:
    """用户令牌写共享层之前的门槛。服务令牌直接放行。"""
    if identity.is_service:
        return
    owner = identity.user_id or ""
    if await has_subscription(owner, group_id, sender_id):
        return
    raise HTTPException(
        status_code=403,
        detail=(
            f"你还没有订阅这个来源（群 {group_id}"
            + (f" · 发送者 {sender_id}" if sender_id else "")
            + "），所以不能往共享的原始层写它的消息。"
            "请先在网页上（或发 /订阅）把这个来源订上 —— 订阅决定了抽什么。"
        ),
    )


class MessageBody(BaseModel):
    model_config = ConfigDict(extra="ignore")

    message_id: str
    group_id: str
    ts: int
    group_name: str | None = None
    sender_id: str = ""
    sender_name: str | None = None
    content: str = ""
    attachments: Any = None
    raw: Any = None


@router.post("/messages")
async def create_message(
    body: MessageBody,
    identity: Annotated[Identity, Depends(require_token)],
):
    """创建原始消息（幂等：`(group_id, message_id)` 唯一）。

    服务令牌（bot）与用户令牌（客户端）都能调，但用户令牌要先证明这个来源是他
    订阅的 —— 见上面那段说明。
    """
    await _require_subscribed(identity, body.group_id, body.sender_id or None)
    raw_id, is_new = await insert_raw_message(
        message_id=body.message_id,
        group_id=body.group_id,
        group_name=body.group_name,
        sender_id=body.sender_id,
        sender_name=body.sender_name,
        ts=body.ts,
        content=body.content,
        attachments=body.attachments if body.attachments is not None else [],
        raw=body.raw if body.raw is not None else {},
    )
    logger.info(
        "原始消息落库：group=%s msg_id=%s is_new=%s 预览=%s",
        body.group_id,
        body.message_id,
        is_new,
        preview(body.content),
    )
    return {"id": raw_id, "is_new": is_new}


@router.get("/messages", dependencies=[WriteDep])
async def get_messages(
    state: list[str] | None = Query(default=None, description="可重复或逗号分隔"),
    group_id: str | None = Query(default=None),
    since: int | None = Query(default=None, description="ts 毫秒，只返回 ts >= since"),
    limit: int = Query(default=100, ge=1, le=1000),
    count_only: str | None = Query(default=None),
):
    """**服务令牌专属**：共享层里是所有人订阅的所有群的消息，用户读它就是跨群泄露。"""
    states = _multi(state)
    if _flag(count_only):
        return {"count": await count_messages(states=states, group_id=group_id, since=since)}
    rows = await list_messages(states=states, group_id=group_id, since=since, limit=limit)
    return {"messages": raw_views(rows)}


@router.get("/messages/{raw_id}", dependencies=[WriteDep])
async def get_message(raw_id: str):
    """**服务令牌专属**（同理：共享层的读一律不给用户令牌）。"""
    row = await get_raw(raw_id)
    if row is None:
        raise HTTPException(status_code=404, detail="原始消息不存在")
    return raw_view(row)


class MessagePatch(BaseModel):
    model_config = ConfigDict(extra="allow")

    state: str | None = None
    state_reason: str | None = None
    attachments: Any = None


@router.patch("/messages/{raw_id}")
async def patch_message(
    raw_id: str,
    body: MessagePatch,
    identity: Annotated[Identity, Depends(require_token)],
):
    """只允许改 `state` / `state_reason` / `attachments`。

    `attachments` 是"事后补齐"（先落库消息本体，再下载、上传、回填），
    不是修改本体。`content` / `raw` / `ts` / `message_id` / `group_id` /
    `sender_id` 一律忽略 —— 传了也不会写进去，响应里的最终值就是证据。

    用户令牌要对**那条原文自己的来源**有订阅才能改：门槛按行里的
    (group_id, sender_id) 判，而不是按请求体 —— 请求体里根本没有这些字段。
    """
    provided = body.model_dump(exclude_unset=True)
    ignored = sorted(k for k in provided if k not in RAW_PATCHABLE)

    existing = await get_raw(raw_id)
    if existing is None:
        raise HTTPException(status_code=404, detail="原始消息不存在")
    await _require_subscribed(
        identity,
        str(existing.get("group_id") or ""),
        str(existing.get("sender_id") or "") or None,
    )

    fields: dict[str, Any] = {}
    if "state" in provided:
        fields["state"] = "" if provided["state"] is None else str(provided["state"])
    if "state_reason" in provided:
        value = provided["state_reason"]
        fields["state_reason"] = None if value is None else str(value)
    if "attachments" in provided:
        fields["attachments"] = _json_text(provided["attachments"], [])

    await patch_raw_message(raw_id, fields)
    if ignored:
        logger.info("PATCH /messages/%s 忽略了不可改字段：%s", raw_id, ignored)
    return raw_view(await get_raw(raw_id))


# --------------------------------------------------------------------------
# 2. 通知 notification
# --------------------------------------------------------------------------


class NotificationBody(BaseModel):
    model_config = ConfigDict(extra="ignore")

    raw_message_id: str
    group_id: str
    source_ts: int
    title: str
    # 故意放宽成 Any：缺失 / null / 空串 / 纯空白都要走我们自己的 400，
    # 而不是 pydantic 的 422 —— 契约只规定了一种错误："没有证据的条目不许入库"
    evidence: Any = None
    group_name: str | None = None
    sender_id: str | None = None
    sender_name: str | None = None
    summary: str | None = None
    location: str | None = None
    due_at: int | None = None
    due_text: str | None = None
    due_confidence: float = 0.0
    conflict: bool = False
    candidates: Any = None
    extractor: str | None = None
    model: str | None = None
    prompt_ver: str | None = None


@router.post("/notifications")
async def create_notification(
    body: NotificationBody,
    identity: Annotated[Identity, Depends(require_token)],
    user_id: str | None = Query(default=None, description="服务令牌必须显式指定归属用户"),
):
    """创建或更新通知（幂等：`(user_id, raw_message_id)` 唯一）。

    **按用户扇出**：同一个 raw_message 被 N 个用户订阅，bot 就在这里写 N 次、
    每次带不同的 user_id。抽取只跑一次，这里只是把结果分发给各人。

    用户令牌也能调（每个人的客户端写自己那份），此时归属被强制成他自己。

    这里**不要求**"已订阅这个来源"：通知是"我已经收到的东西"，订阅是"我以后要收
    什么"。退订之后不该连历史通知都更新不了，而且重跑抽取时那条通知本来就还在。

    `evidence` 为空直接 400 —— 这是后端替入库方守住的硬约束：
    没有证据的条目不许入库（防的是模型幻觉出一条无据的任务）。
    """
    owner = resolve_owner(identity, user_id)
    evidence = "" if body.evidence is None else str(body.evidence)
    if not evidence.strip():
        raise HTTPException(
            status_code=400,
            detail="没有证据的条目不许入库：evidence 不能为空",
        )

    notif_id, created = await upsert_notification(
        owner,
        {
            "raw_message_id": body.raw_message_id,
            "group_id": body.group_id,
            "group_name": body.group_name,
            "sender_id": body.sender_id,
            "sender_name": body.sender_name,
            "source_ts": body.source_ts,
            "title": body.title,
            "summary": body.summary,
            "location": body.location,
            "due_at": body.due_at,
            "due_text": body.due_text,
            "due_confidence": body.due_confidence,
            "evidence": evidence,
            "conflict": body.conflict,
            "candidates": body.candidates if isinstance(body.candidates, list) else [],
            "extractor": body.extractor,
            "model": body.model,
            "prompt_ver": body.prompt_ver,
        },
    )
    logger.info(
        "通知落库：id=%s user=%s created=%s 标题=%s",
        notif_id,
        owner,
        created,
        preview(body.title),
    )
    return {"id": notif_id, "created": created}


@router.get("/notifications")
async def get_notifications(
    identity: Annotated[Identity, Depends(require_token)],
    user_id: str | None = Query(default=None, description="服务令牌必须显式指定"),
    since: int | None = Query(default=None, description="只返回 updated_at > since 的行"),
    status: str = Query(default="all"),
    q: str | None = Query(default=None),
    limit: int = Query(default=500, ge=1, le=2000),
    count_only: str | None = Query(default=None),
):
    """通知列表（**读投影**：人工修正已覆盖、status 已推导）。

    用户令牌只能看到自己的；服务令牌要按用户读（bot 发摘要时逐个用户拉）。
    """
    owner = resolve_owner(identity, user_id)
    if _flag(count_only):
        return {
            "count": await count_notification_views(
                owner, since=since, status=status, q=q
            )
        }
    views = await list_notification_views(
        owner, since=since, status=status, q=q, limit=limit
    )
    return {"server_time": now_ms(), "notifications": views}


@router.get("/notifications/{notif_id}")
async def get_notification_detail(
    notif_id: str,
    identity: Annotated[Identity, Depends(require_token)],
    user_id: str | None = Query(default=None),
):
    owner = resolve_owner(identity, user_id)
    view = await get_notification_view(owner, notif_id)
    if view is None:
        # 别人的条目也走这条 —— 不区分"不存在"和"不是你的"，
        # 否则可以用它探测"这个 id 存在吗"。
        raise HTTPException(status_code=404, detail="通知不存在")
    row = await get_notification_row(notif_id, owner)
    raw = await get_raw(row["raw_message_id"]) if row else None
    return {"notification": view, "raw": raw_view(raw, owner)}


class NotificationPatch(BaseModel):
    model_config = ConfigDict(extra="allow")

    title: str | None = None
    summary: str | None = None
    location: str | None = None
    due_at: int | None = None
    due_text: str | None = None
    due_confidence: float | None = None
    evidence: str | None = None
    conflict: bool | None = None
    candidates: Any = None
    model: str | None = None
    prompt_ver: str | None = None


@router.patch("/notifications/{notif_id}", dependencies=[WriteDep])
async def patch_notification_route(
    notif_id: str,
    body: NotificationPatch,
    identity: Annotated[Identity, Depends(require_token)],
    user_id: str | None = Query(default=None),
):
    """bot 重跑时改机器字段。

    **不允许改 `status` 和 `read`** —— 那两个只能走 corrections 与 /read，
    因为要留痕。传了会被忽略，响应里的最终值就是它们现在的真实取值。
    """
    owner = resolve_owner(identity, user_id)
    if await get_notification_row(notif_id, owner) is None:
        raise HTTPException(status_code=404, detail="通知不存在")

    provided = body.model_dump(exclude_unset=True)
    ignored = sorted(k for k in provided if k not in NOTIFICATION_PATCHABLE)

    fields: dict[str, Any] = {}
    for key, value in provided.items():
        if key not in NOTIFICATION_PATCHABLE:
            continue
        if key == "candidates":
            fields[key] = _json_text(value, [])
        elif key == "conflict":
            fields[key] = int(bool(value))
        elif key == "due_confidence":
            fields[key] = 0.0 if value is None else float(value)
        elif key == "due_at":
            fields[key] = as_int(value)
        elif key == "evidence":
            if not str(value or "").strip():
                raise HTTPException(
                    status_code=400,
                    detail="没有证据的条目不许入库：evidence 不能为空",
                )
            fields[key] = str(value)
        elif key == "title":
            fields[key] = "" if value is None else str(value)
        else:
            fields[key] = None if value is None else str(value)

    await patch_notification(notif_id, owner, fields)
    if ignored:
        logger.info("PATCH /notifications/%s 忽略了不可改字段：%s", notif_id, ignored)
    return await get_notification_view(owner, notif_id)


@router.delete("/notifications/{notif_id}", dependencies=[WriteDep])
async def delete_notification_route(
    notif_id: str,
    identity: Annotated[Identity, Depends(require_token)],
    user_id: str | None = Query(default=None),
):
    owner = resolve_owner(identity, user_id)
    if not await delete_notification(notif_id, owner):
        raise HTTPException(status_code=404, detail="通知不存在")
    return {"deleted": True}


class CorrectionBody(BaseModel):
    model_config = ConfigDict(extra="ignore")

    field: str
    value: Any = None
    # 谁操作的（界面上显示"谁改的"），**不是**租户 —— 租户从令牌来
    actor: str = "web"


@router.post("/notifications/{notif_id}/corrections")
async def create_correction(
    notif_id: str,
    body: CorrectionBody,
    identity: Annotated[Identity, Depends(require_token)],
    user_id: str | None = Query(default=None),
):
    """人工修正（只追加）。展示时它会覆盖 notification 里的机器值。

    这是**用户**能做的写操作之一（另一个是已读），所以不挂 WriteDep：
    网页令牌本来就要能改自己条目的解读。
    """
    owner = resolve_owner(identity, user_id)
    if body.field not in CORRECTABLE_FIELDS:
        raise HTTPException(
            status_code=400,
            detail=f"不可修正的字段：{body.field}（只能是 {list(CORRECTABLE_FIELDS)}）",
        )
    if await get_notification_row(notif_id, owner) is None:
        raise HTTPException(status_code=404, detail="通知不存在")

    value = body.value
    if body.field == "due_at":
        if value in (None, "", "null"):
            value = None
        else:
            parsed = as_int(value)
            if parsed is None:
                raise HTTPException(status_code=400, detail="due_at 必须是毫秒时间戳或 null")
            value = parsed
    elif body.field == "status":
        if value not in VALID_STATUS:
            raise HTTPException(
                status_code=400, detail=f"status 只能是 {sorted(VALID_STATUS)}"
            )
    else:
        value = "" if value is None else str(value)

    await add_correction(notif_id, owner, body.field, value, body.actor or "web")
    return {"ok": True, "notification": await get_notification_view(owner, notif_id)}


@router.get("/notifications/{notif_id}/corrections")
async def get_corrections(
    notif_id: str,
    identity: Annotated[Identity, Depends(require_token)],
    user_id: str | None = Query(default=None),
):
    owner = resolve_owner(identity, user_id)
    if await get_notification_row(notif_id, owner) is None:
        raise HTTPException(status_code=404, detail="通知不存在")
    return {"corrections": await list_corrections(notif_id, owner)}


class ReadBody(BaseModel):
    model_config = ConfigDict(extra="ignore")

    read: bool = True


@router.post("/notifications/{notif_id}/read")
async def mark_read(
    notif_id: str,
    body: ReadBody,
    identity: Annotated[Identity, Depends(require_token)],
    user_id: str | None = Query(default=None),
):
    owner = resolve_owner(identity, user_id)
    if await get_notification_row(notif_id, owner) is None:
        raise HTTPException(status_code=404, detail="通知不存在")
    await set_read(notif_id, owner, body.read)
    return {"read": body.read}


# --------------------------------------------------------------------------
# 3. 附件 attachment
# --------------------------------------------------------------------------


@router.post("/attachments")
async def upload_attachment(request: Request):
    """`multipart/form-data`：`file` / `filename` / `source_url`。

    服务令牌与用户令牌都能调：每个人的客户端也要上传自己读到的证据图。
    `attachment` 表没有归属（**字节只存一份**，访问权由签发时绑定 user_id 的
    签名 URL 决定），所以这里没有"这是不是他的"可以检查。

    ⚠️ 代价说清楚：拿到任何有效令牌的人都能反复上传，唯一的闸是
    `MEDIA_MAX_BYTES`（单文件上限）。这套部署本来就是邀请制的小范围使用，
    所以先接受这个代价；要收紧就得给每个用户加配额。
    """
    try:
        return await handle_upload(request)
    except TooLarge as exc:
        raise HTTPException(status_code=413, detail=str(exc))
    except MultipartError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


async def require_download(
    att_id: str,
    exp: Annotated[str | None, Query()] = None,
    sig: Annotated[str | None, Query()] = None,
    u: Annotated[str | None, Query()] = None,
    identity: Annotated[Identity | None, Depends(optional_token)] = None,
) -> None:
    """附件下载的鉴权：**有效签名 URL 或有效 Bearer 令牌**，任一即可。

    这是全项目**唯一**允许不带 Authorization 头的接口，因为浏览器用
    `<img src>` / `<a href>` 取附件时根本带不了那个头。签名由 signing.py 发：
    读投影里每次现签、带过期时间，而且**绑定了 user_id**（签在 `u=` 里）。

    三条放行路径：
      1. 没配 API_TOKEN（本地开发）—— 与其它接口一致，不校验
      2. 带了有效 Bearer
      3. `u` + `exp` + `sig` 签名有效且未过期

    都不满足 → 401。绑 user_id 的意义：拿到别人通知里那条链接的人，
    验签过不去 —— 链接看起来完全正常，但只对签给它的人有效。
    """
    if not get_settings().auth_enabled:
        return
    if identity is not None:
        return
    if u and verify_attachment_sig(u, att_id, exp, sig):
        return
    raise HTTPException(
        status_code=401,
        detail="附件需要有效的 Authorization: Bearer <令牌>，或未过期的签名链接",
        headers={"WWW-Authenticate": "Bearer"},
    )


@open_router.get("/attachments/{att_id}")
async def download_attachment(att_id: str, _: None = Depends(require_download)):
    loaded = await load_bytes(att_id)
    if loaded is None:
        raise HTTPException(status_code=404, detail="附件不存在或文件已丢失")
    row, data = loaded
    content_type = row.get("content_type") or "application/octet-stream"
    return Response(
        content=data,
        media_type=content_type,
        headers={
            "Content-Disposition": content_disposition(row.get("filename"), content_type),
            "Content-Length": str(len(data)),
            # 附件内容来自外部，绝不让浏览器按嗅探出来的类型执行它
            "X-Content-Type-Options": "nosniff",
        },
    )


# --------------------------------------------------------------------------
# 3b. 用户、注册、邀请码
# --------------------------------------------------------------------------

# ⚠️ 这一节是**鉴权之外**的唯一入口（`/api/register` 挂在 public_router 上）。
# 它的安全完全由三样东西担着：邀请码、QQ 验证码（bot 只发给能收到它消息的人）、
# 以及猜错次数上限。改动这里之前先想清楚这三点还在不在。


class VerifyRequestBody(BaseModel):
    model_config = ConfigDict(extra="ignore")

    qq: str


@router.post("/verify/request", dependencies=[WriteDep])
async def request_verify_code(body: VerifyRequestBody):
    """给某个 QQ 生成验证码。

    **只允许服务令牌（bot）调**。原因：如果谁能调，他就能一直刷新别人的验证码，
    把真正的主人挡在门外（拒绝服务），也把 6 位码的猜测窗口拉长。

    bot 拿到码之后**自己用 QQ 私聊发给对方** —— 这是整条链路的信任基础：
    只有能收到那条私聊的人，才证明得了自己拥有这个 QQ 号。
    """
    try:
        issued = await issue_verify_code(body.qq)
    except UserError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail)
    logger.info("签发验证码 qq=%s", issued["qq"])
    return issued


class RegisterBody(BaseModel):
    model_config = ConfigDict(extra="ignore")

    qq: str
    code: str
    invite_code: str | None = None
    display_name: str | None = None


@public_router.post("/register")
async def register(body: RegisterBody):
    """注册新用户，或给已有用户**轮换令牌**。

    **不需要任何令牌**（注册的前提就是还没有），靠邀请码 + QQ 验证码把关。

    返回值里的 `token` 是**明文令牌，只会出现这一次** —— 库里只存 sha256。
    前端必须让用户当场复制走，并明确告诉他丢了只能用同样的流程再换一个。
    """
    try:
        user, token, created = await register_or_rotate(
            qq=body.qq,
            code=body.code,
            invite_code=body.invite_code,
            display_name=body.display_name,
        )
    except UserError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail)

    return {
        "user": public_user(user),
        "token": token,
        "created": created,
        "notice": (
            "这个令牌只会显示这一次，请立刻保存。它同时是你的登录凭证和调用凭证。"
            "丢了可以用同样的方式（QQ 找机器人要验证码）再换一个。"
        ),
    }


@router.get("/me")
async def whoami(identity: Annotated[Identity, Depends(require_token)]):
    """我是谁。前端登录后第一件事就是调它 —— 令牌对不对一次就知道。"""
    if identity.is_service:
        return {"scope": "service", "user": None}
    user = await get_user_by_id(identity.user_id or "")
    if user is None:
        raise HTTPException(status_code=401, detail="令牌无效")
    return {"scope": "user", "user": public_user(user)}


class InviteBody(BaseModel):
    model_config = ConfigDict(extra="ignore")

    note: str | None = None
    max_uses: int = 1
    ttl_seconds: int | None = None


@router.post("/invites", dependencies=[WriteDep])
async def create_invite_route(body: InviteBody):
    """发一个邀请码。**只有服务令牌能发** —— 它就是"谁能注册"的开关。

    运营者用 API_TOKEN 调它（curl 或前端的管理入口），把 code 发给要邀请的人。
    """
    invite = await create_invite(
        note=body.note, max_uses=body.max_uses, ttl_seconds=body.ttl_seconds
    )
    logger.info("签发邀请码 code=%s note=%s max_uses=%s", invite["code"], body.note, invite["max_uses"])
    return invite


@router.get("/invites", dependencies=[WriteDep])
async def list_invites_route():
    return {"invites": await list_invites()}


@router.get("/users", dependencies=[WriteDep])
async def list_users_route():
    """所有用户。**不发令牌、不发摘要**，只是名单。"""
    return {"users": await list_users(), "count": await count_users()}


@router.get("/users/lookup", dependencies=[WriteDep])
async def lookup_user_route(qq: str = Query(...)):
    """按 QQ 号查用户。服务令牌专属。

    这是 bot 的**身份解析**入口：QQ 侧的一切身份锚点都是 QQ 号（谁发的消息、
    谁发的指令），而数据层的租户是 `user_id`。少了这一步，bot 就只能靠
    "QQ 号当 user_id 用" —— 那正好是串数据的经典写法。

    查不到返回 404 而不是空对象：调用方（bot）需要能区分"这个人还没注册"
    和"后端没答上来"，前者要提示他去注册，后者要保持 pending 重试。
    """
    user = await get_user_by_qq(qq)
    if user is None:
        raise HTTPException(status_code=404, detail="这个 QQ 还没有注册")
    return {"user": public_user(user)}


# --------------------------------------------------------------------------
# 4. 群状态 group_state
# --------------------------------------------------------------------------


class GroupBody(BaseModel):
    model_config = ConfigDict(extra="ignore")

    group_id: str
    last_msg_ts: int
    group_name: str | None = None


@router.post("/groups")
async def upsert_group_route(
    body: GroupBody,
    identity: Annotated[Identity, Depends(require_token)],
):
    """upsert 群状态。

    响应里带 `previous_last_msg_ts`（更新**前**的值）—— 入库方用它做缺口检测，
    省掉"先读再写"那一次竞态。

    用户令牌要证明自己订阅了这个群里**至少一个**发送者（`sender_id=None`
    那一档）：群状态是共享的，往它写就是往所有人看到的那张表写。
    """
    await _require_subscribed(identity, body.group_id, None)
    return await upsert_group(body.group_id, body.group_name, body.last_msg_ts)


@router.get("/groups", dependencies=[WriteDep])
async def get_groups():
    """**服务令牌专属**：群列表是全站的（所有用户订阅的所有群）。"""
    return {"groups": await list_groups()}


# --------------------------------------------------------------------------
# 5. 缺口告警 gap_alert
# --------------------------------------------------------------------------


class GapAlertBody(BaseModel):
    model_config = ConfigDict(extra="ignore")

    group_id: str
    from_ts: int
    to_ts: int
    group_name: str | None = None
    reason: str | None = None


@router.post("/gap-alerts")
async def create_gap_alert(
    body: GapAlertBody,
    identity: Annotated[Identity, Depends(require_token)],
    user_id: str | None = Query(default=None),
):
    owner = resolve_owner(identity, user_id)
    alert_id = await add_gap_alert(
        owner, body.group_id, body.group_name, body.from_ts, body.to_ts, body.reason
    )
    return {"id": alert_id}


@router.get("/gap-alerts")
async def get_gap_alerts(
    identity: Annotated[Identity, Depends(require_token)],
    user_id: str | None = Query(default=None),
    acknowledged: str | None = Query(default=None),
    limit: int = Query(default=20, ge=1, le=1000),
):
    owner = resolve_owner(identity, user_id)
    rows = await list_gap_alerts(
        owner, acknowledged=_optional_flag(acknowledged, "acknowledged"), limit=limit
    )
    return {"alerts": [{**row, "acknowledged": bool(row.get("acknowledged"))} for row in rows]}


@router.post("/gap-alerts/{alert_id}/ack")
async def ack_gap_alert_route(
    alert_id: str,
    identity: Annotated[Identity, Depends(require_token)],
    user_id: str | None = Query(default=None),
):
    owner = resolve_owner(identity, user_id)
    if not await ack_gap_alert(alert_id, owner):
        raise HTTPException(status_code=404, detail="缺口告警不存在")
    return {"acknowledged": True}


# --------------------------------------------------------------------------
# 6. 统计 pipeline_stat（后端只做累加）
# --------------------------------------------------------------------------


class StatsBody(BaseModel):
    model_config = ConfigDict(extra="ignore")

    day: str | None = None
    fields: dict[str, Any] = Field(default_factory=dict)


@router.post("/stats")
async def create_stats(
    body: StatsBody,
    identity: Annotated[Identity, Depends(require_token)],
    user_id: str | None = Query(default=None),
):
    owner = resolve_owner(identity, user_id)
    increments: dict[str, int] = {}
    for key, value in body.fields.items():
        if key not in STAT_FIELDS:
            continue  # 未知键忽略（契约：未知字段忽略，不报错）
        parsed = as_int(value)
        if parsed is None:
            raise HTTPException(status_code=400, detail=f"{key} 必须是整数")
        increments[key] = parsed
    return await add_stats(owner, body.day, increments)


@router.get("/stats")
async def get_stats(
    identity: Annotated[Identity, Depends(require_token)],
    user_id: str | None = Query(default=None),
    day: str | None = Query(default=None),
):
    return await get_stat(resolve_owner(identity, user_id), day)


# --------------------------------------------------------------------------
# 7. digest 发送记录（契约 §10）
# --------------------------------------------------------------------------


class DigestLogBody(BaseModel):
    model_config = ConfigDict(extra="ignore")

    kind: str
    day: str | None = None
    text: str = ""
    sent: bool = False
    error: str | None = None


@router.post("/digest-log", dependencies=[WriteDep])
async def create_digest_log(
    body: DigestLogBody,
    identity: Annotated[Identity, Depends(require_token)],
    user_id: str | None = Query(default=None),
):
    """记录一次发送（幂等键 `(user_id, day, kind, sent)`，见 db.add_digest_log）。

    `kind` 的取值由 bot 定义，后端只存字符串。
    **摘要记录必须按用户分开**：否则"今天给 A 发过没有"会被 B 的记录顶掉。
    """
    owner = resolve_owner(identity, user_id)
    log_id, created = await add_digest_log(
        owner,
        day=body.day,
        kind=body.kind,
        text=body.text,
        sent=body.sent,
        error=body.error,
    )
    logger.info(
        "digest 记录：id=%s user=%s created=%s kind=%s sent=%s",
        log_id,
        owner,
        created,
        body.kind,
        body.sent,
    )
    return {"id": log_id}


@router.get("/digest-log")
async def get_digest_log(
    identity: Annotated[Identity, Depends(require_token)],
    user_id: str | None = Query(default=None),
    day: str | None = Query(default=None),
    kind: str | None = Query(default=None),
    sent: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=1000),
    count_only: str | None = Query(default=None),
):
    owner = resolve_owner(identity, user_id)
    sent_flag = _optional_flag(sent, "sent")
    if _flag(count_only):
        return {
            "count": await count_digest_logs(owner, day=day, kind=kind, sent=sent_flag)
        }
    rows = await list_digest_logs(owner, day=day, kind=kind, sent=sent_flag, limit=limit)
    return {"logs": [{**row, "sent": bool(row.get("sent"))} for row in rows]}


# --------------------------------------------------------------------------
# 8. bot 的键值暂存（契约 §11）
#
# 后端不理解 value 的含义：它只是一块带 TTL 的持久化草稿纸。
# --------------------------------------------------------------------------


class StateBody(BaseModel):
    value: Any
    ttl_seconds: int | None = None


@router.put("/state/{namespace}/{key}")
async def put_state_route(
    namespace: str,
    key: str,
    body: StateBody,
    identity: Annotated[Identity, Depends(require_token)],
    user_id: str | None = Query(default=None),
):
    owner = resolve_owner(identity, user_id)
    expires_at = None
    if body.ttl_seconds is not None:
        ttl = as_int(body.ttl_seconds)
        if ttl is None or ttl < 0:
            raise HTTPException(status_code=400, detail="ttl_seconds 必须是非负整数")
        expires_at = now_ms() + ttl * 1000

    await put_state(owner, namespace, key, _json_text(body.value, None), expires_at)
    return {"ok": True, "expires_at": expires_at}


@router.get("/state/{namespace}/{key}")
async def get_state_route(
    namespace: str,
    key: str,
    identity: Annotated[Identity, Depends(require_token)],
    user_id: str | None = Query(default=None),
):
    """已过期或不存在一律 404 —— 过期判定在读取时做，不依赖后台清理。"""
    row = await get_state(resolve_owner(identity, user_id), namespace, key)
    if row is None:
        raise HTTPException(status_code=404, detail="键不存在或已过期")
    return {
        "key": row["key"],
        "value": json_loads(row.get("value"), None),
        "expires_at": row.get("expires_at"),
    }


@router.delete("/state/{namespace}/{key}")
async def delete_state_route(
    namespace: str,
    key: str,
    identity: Annotated[Identity, Depends(require_token)],
    user_id: str | None = Query(default=None),
):
    """删除是幂等的：键本来就不存在也返回成功（后置条件都成立）。"""
    await delete_state(resolve_owner(identity, user_id), namespace, key)
    return {"deleted": True}


@router.get("/state/{namespace}")
async def list_state_route(
    namespace: str,
    identity: Annotated[Identity, Depends(require_token)],
    user_id: str | None = Query(default=None),
    count_only: str | None = Query(default=None),
):
    rows = await list_state(resolve_owner(identity, user_id), namespace)
    if _flag(count_only):
        return {"count": len(rows)}
    return {
        "items": [
            {
                "key": row["key"],
                "value": json_loads(row.get("value"), None),
                "expires_at": row.get("expires_at"),
            }
            for row in rows
        ]
    }


# --------------------------------------------------------------------------
# 9. 订阅 subscription（契约 §12）
#
# 这是多用户之后**用户唯一能改的东西**：他订哪些 (群, 发送者)。
# bot 处理所有用户订阅的并集，抽一次，再按订阅扇出。
# --------------------------------------------------------------------------


class SubscriptionBody(BaseModel):
    model_config = ConfigDict(extra="ignore")

    group_id: str
    sender_id: str
    group_name: str | None = None
    sender_name: str | None = None
    note: str | None = None


class SubscriptionPatchBody(BaseModel):
    model_config = ConfigDict(extra="ignore")

    enabled: bool | None = None
    note: str | None = None
    group_name: str | None = None
    sender_name: str | None = None


@router.get("/subscriptions")
async def list_subscriptions_route(
    identity: Annotated[Identity, Depends(require_token)],
    user_id: str | None = Query(default=None),
    include_disabled: str | None = Query(default=None),
):
    """我的订阅。用户令牌看自己，服务令牌要显式带 user_id。"""
    owner = resolve_owner(identity, user_id)
    rows = await subscriptions.list_for_user(
        owner, include_disabled=include_disabled is None or _flag(include_disabled)
    )
    return {"subscriptions": rows, "count": len(rows)}


@router.post("/subscriptions")
async def add_subscription_route(
    body: SubscriptionBody,
    identity: Annotated[Identity, Depends(require_token)],
    user_id: str | None = Query(default=None),
):
    """订一个 (群, 发送者)。

    **刻意不挂 `WriteDep`**：这是用户自己配置自己的东西，用 UserToken 就该能改
    （和 corrections / read 一致）。服务令牌也能调，但必须显式带 user_id ——
    那是 bot 处理 QQ 侧 `/订阅` 指令时用的路径。

    `sender_id` 是**必填**的，而且不接受 `*` 之类的通配符 —— 订阅的最小单位
    就是"某个群里某个人说的话"，没有"订整个群"这个选项。
    理由和挡住它的三层在 subscriptions.py 里。
    """
    owner = resolve_owner(identity, user_id)
    try:
        sub, created = await subscriptions.add(
            owner,
            group_id=body.group_id,
            sender_id=body.sender_id,
            group_name=body.group_name,
            sender_name=body.sender_name,
            note=body.note,
        )
    except subscriptions.SubscriptionError as exc:
        raise HTTPException(status_code=400, detail=exc.message) from exc
    return {"subscription": sub, "created": created}


@router.patch("/subscriptions/{sub_id}")
async def patch_subscription_route(
    sub_id: str,
    body: SubscriptionPatchBody,
    identity: Annotated[Identity, Depends(require_token)],
    user_id: str | None = Query(default=None),
):
    owner = resolve_owner(identity, user_id)
    try:
        sub = await subscriptions.patch(
            owner, sub_id, body.model_dump(exclude_none=True)
        )
    except subscriptions.SubscriptionError as exc:
        raise HTTPException(status_code=400, detail=exc.message) from exc
    if sub is None:
        # 404 而不是 403：别人的 id 探测不出来（和通知详情一致）
        raise HTTPException(status_code=404, detail="订阅不存在")
    return {"subscription": sub}


@router.delete("/subscriptions/{sub_id}")
async def delete_subscription_route(
    sub_id: str,
    identity: Annotated[Identity, Depends(require_token)],
    user_id: str | None = Query(default=None),
):
    owner = resolve_owner(identity, user_id)
    if not await subscriptions.remove(owner, sub_id):
        raise HTTPException(status_code=404, detail="订阅不存在")
    return {"deleted": True}


@router.get("/subscriptions/routing")
async def routing_route(
    identity: Annotated[Identity, Depends(require_service)],
    group_id: str = Query(...),
    sender_id: str | None = Query(default=None),
):
    """**投递名单**：这条消息要扇给谁。服务令牌专属。

    bot 每处理完一条消息就调它一次，拿到 user_id 列表，然后给每个人写一条
    自己的通知。抽一次、扇多次 —— "并集处理"落地的地方。

    `sender_id` 省略 = "这个群里任何发送者"。只有缺口告警用它：缺口是**群级**
    事件（"这个群中间断了一段"），凡是订了这个群里任何人的用户都该知道。
    两种语义不要混：正常投递必须给 sender_id，否则就成了"订整个群"。

    **必须显式要服务令牌**：router 级别的 Bearer 只保证"有身份"，一个普通
    用户拿着自己的令牌就能看到全局投递名单（谁订了哪个来源），那是别人的
    订阅关系。所以这里加 `require_service`，不是靠路由分组。

    放在 `/subscriptions/routing` 而不是 `/routing`：它属于订阅这一块。
    这里刻意**没有** `GET /subscriptions/{sub_id}`，所以 "routing" 不会被
    当成一个 sub_id 吃掉（PATCH/DELETE 是同路径不同方法，不冲突）。
    """
    _ = identity
    return {"user_ids": await subscriptions.subscribers_for(group_id, sender_id)}


@router.get("/sources")
async def list_sources_route(
    identity: Annotated[Identity, Depends(require_token)],
    keyword: str | None = Query(default=None),
    limit: int = Query(default=subscriptions.SOURCE_LIMIT, ge=1, le=1000),
):
    """**信息源目录**：可以订的 (群, 发送者)。

    任何登录用户都能看 —— 新用户注册完手上是空的，没有这份目录就无从订阅。
    目录从共享层聚合，不含任何按用户的数据。
    """
    _ = identity  # 只是为了强制要求登录；目录本身与身份无关
    rows = await subscriptions.list_sources(keyword=keyword, limit=limit)
    return {"sources": rows, "count": len(rows)}


# --------------------------------------------------------------------------
# 10. 健康：只报存储自身
# --------------------------------------------------------------------------


def _storage_writable() -> bool:
    """真的写一个探针文件 —— `os.access` 在 Windows 上不可信。"""
    directory = get_settings().resolved_db_path.parent
    probe = directory / f".write-probe-{os.getpid()}"
    try:
        probe.write_text("ok", encoding="utf-8")
        probe.unlink(missing_ok=True)
        return True
    except OSError:
        return False


@router.get("/health")
async def health(
    identity: Annotated[Identity, Depends(require_token)],
    user_id: str | None = Query(default=None),
):
    """只报存储自身；QQ 连接、抽取流水线那些状态全在 bot 那边。

    计数按用户算：状态页给用户看的是"你收了多少"，不是全站。

    服务令牌可以**不带** user_id —— 那就是纯存活探针（Dockerfile 的 HEALTHCHECK
    就是这么调的，它拿不到 user_id）。那种情况下 `counts` 直接返回 None 而不是
    去查"所有用户"：探针不该顺带做一次无归属查询。
    """
    owner = resolve_owner_optional(identity, user_id)
    settings = get_settings()
    storage_ok = True
    try:
        await fetch_one("SELECT 1 AS ok")
    except Exception:  # pragma: no cover - 只有磁盘/权限坏了才会走到
        logger.exception("存储自检失败")
        storage_ok = False
    writable = _storage_writable()

    counts = None
    if owner is not None:
        counts = {
            "messages": await count_messages(),
            "notifications": await count_notifications(owner),
            "attachments": await count_attachments(),
        }

    return {
        "ok": storage_ok and writable,
        "server_time": now_ms(),
        "user_id": owner,
        "storage": {
            "driver": "sqlite",
            "path": str(settings.resolved_db_path),
            "writable": writable,
        },
        "counts": counts,
        "version": __version__,
    }
