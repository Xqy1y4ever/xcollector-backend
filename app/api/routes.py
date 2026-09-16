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
import secrets
from typing import Any

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
from ..config import get_settings
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
from ..utils import as_int, json_loads, now_ms, preview

logger = logging.getLogger(__name__)

# done = 做完了，archived = 不是通知/误报。这是契约 §2 明确规定的取值集合，
# 属于"接口参数校验"，不是后端在判断业务。
VALID_STATUS = {"active", "archived", "done"}

_TRUE = {"1", "true", "yes", "on", "t"}
_FALSE = {"0", "false", "no", "off", "f", ""}

_warned_open = False


# --------------------------------------------------------------------------
# 认证：所有 /api 请求都要带 Authorization: Bearer <API_TOKEN>
# --------------------------------------------------------------------------


async def require_token(authorization: str | None = Header(default=None)) -> None:
    """校验共享密钥。

    `API_TOKEN` 为空 = 不校验（仅本地开发），但会打 WARNING ——
    "忘了配"和"故意不配"在日志里必须能区分开。
    """
    global _warned_open
    settings = get_settings()
    if not settings.auth_enabled:
        if not _warned_open:
            _warned_open = True
            logger.warning(
                "API_TOKEN 未配置：本服务不校验任何请求的 Authorization 头（仅限本地开发）"
            )
        return

    scheme, _, token = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise HTTPException(
            status_code=401,
            detail="缺少 Authorization: Bearer <API_TOKEN>",
            headers={"WWW-Authenticate": "Bearer"},
        )
    if not secrets.compare_digest(token.strip(), settings.api_token.strip()):
        raise HTTPException(
            status_code=401,
            detail="API_TOKEN 无效",
            headers={"WWW-Authenticate": "Bearer"},
        )


router = APIRouter(prefix="/api", dependencies=[Depends(require_token)])


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
async def create_message(body: MessageBody):
    """创建原始消息（幂等：`(group_id, message_id)` 唯一）。"""
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


@router.get("/messages")
async def get_messages(
    state: list[str] | None = Query(default=None, description="可重复或逗号分隔"),
    group_id: str | None = Query(default=None),
    since: int | None = Query(default=None, description="ts 毫秒，只返回 ts >= since"),
    limit: int = Query(default=100, ge=1, le=1000),
    count_only: str | None = Query(default=None),
):
    states = _multi(state)
    if _flag(count_only):
        return {"count": await count_messages(states=states, group_id=group_id, since=since)}
    rows = await list_messages(states=states, group_id=group_id, since=since, limit=limit)
    return {"messages": raw_views(rows)}


@router.get("/messages/{raw_id}")
async def get_message(raw_id: str):
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
async def patch_message(raw_id: str, body: MessagePatch):
    """只允许改 `state` / `state_reason` / `attachments`。

    `attachments` 是"事后补齐"（bot 先落库消息本体，再下载、上传、回填），
    不是修改本体。`content` / `raw` / `ts` / `message_id` / `group_id` /
    `sender_id` 一律忽略 —— 传了也不会写进去，响应里的最终值就是证据。
    """
    provided = body.model_dump(exclude_unset=True)
    ignored = sorted(k for k in provided if k not in RAW_PATCHABLE)

    if await get_raw(raw_id) is None:
        raise HTTPException(status_code=404, detail="原始消息不存在")

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
async def create_notification(body: NotificationBody):
    """创建或更新通知（幂等：`raw_message_id` 唯一）。

    `evidence` 为空直接 400 —— 这是后端替 bot 守住的硬约束：
    没有证据的条目不许入库（防的是模型幻觉出一条无据的任务）。
    """
    evidence = "" if body.evidence is None else str(body.evidence)
    if not evidence.strip():
        raise HTTPException(
            status_code=400,
            detail="没有证据的条目不许入库：evidence 不能为空",
        )

    notif_id, created = await upsert_notification(
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
        }
    )
    logger.info(
        "通知落库：id=%s created=%s 标题=%s",
        notif_id,
        created,
        preview(body.title),
    )
    return {"id": notif_id, "created": created}


@router.get("/notifications")
async def get_notifications(
    since: int | None = Query(default=None, description="只返回 updated_at > since 的行"),
    status: str = Query(default="all"),
    q: str | None = Query(default=None),
    limit: int = Query(default=500, ge=1, le=2000),
    count_only: str | None = Query(default=None),
):
    """通知列表（**读投影**：人工修正已覆盖、status 已推导）。"""
    if _flag(count_only):
        return {"count": await count_notification_views(since=since, status=status, q=q)}
    views = await list_notification_views(since=since, status=status, q=q, limit=limit)
    return {"server_time": now_ms(), "notifications": views}


@router.get("/notifications/{notif_id}")
async def get_notification_detail(notif_id: str):
    view = await get_notification_view(notif_id)
    if view is None:
        raise HTTPException(status_code=404, detail="通知不存在")
    row = await get_notification_row(notif_id)
    raw = await get_raw(row["raw_message_id"]) if row else None
    return {"notification": view, "raw": raw_view(raw)}


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


@router.patch("/notifications/{notif_id}")
async def patch_notification_route(notif_id: str, body: NotificationPatch):
    """bot 重跑时改机器字段。

    **不允许改 `status` 和 `read`** —— 那两个只能走 corrections 与 /read，
    因为要留痕。传了会被忽略，响应里的最终值就是它们现在的真实取值。
    """
    if await get_notification_row(notif_id) is None:
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

    await patch_notification(notif_id, fields)
    if ignored:
        logger.info("PATCH /notifications/%s 忽略了不可改字段：%s", notif_id, ignored)
    return await get_notification_view(notif_id)


@router.delete("/notifications/{notif_id}")
async def delete_notification_route(notif_id: str):
    if not await delete_notification(notif_id):
        raise HTTPException(status_code=404, detail="通知不存在")
    return {"deleted": True}


class CorrectionBody(BaseModel):
    model_config = ConfigDict(extra="ignore")

    field: str
    value: Any = None
    user_id: str = "web"


@router.post("/notifications/{notif_id}/corrections")
async def create_correction(notif_id: str, body: CorrectionBody):
    """人工修正（只追加）。展示时它会覆盖 notification 里的机器值。"""
    if body.field not in CORRECTABLE_FIELDS:
        raise HTTPException(
            status_code=400,
            detail=f"不可修正的字段：{body.field}（只能是 {list(CORRECTABLE_FIELDS)}）",
        )
    if await get_notification_row(notif_id) is None:
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

    await add_correction(notif_id, body.field, value, body.user_id or "web")
    return {"ok": True, "notification": await get_notification_view(notif_id)}


@router.get("/notifications/{notif_id}/corrections")
async def get_corrections(notif_id: str):
    if await get_notification_row(notif_id) is None:
        raise HTTPException(status_code=404, detail="通知不存在")
    return {"corrections": await list_corrections(notif_id)}


class ReadBody(BaseModel):
    model_config = ConfigDict(extra="ignore")

    read: bool = True


@router.post("/notifications/{notif_id}/read")
async def mark_read(notif_id: str, body: ReadBody):
    if await get_notification_row(notif_id) is None:
        raise HTTPException(status_code=404, detail="通知不存在")
    await set_read(notif_id, body.read)
    return {"read": body.read}


# --------------------------------------------------------------------------
# 3. 附件 attachment
# --------------------------------------------------------------------------


@router.post("/attachments")
async def upload_attachment(request: Request):
    """`multipart/form-data`：`file` / `filename` / `source_url`。"""
    try:
        return await handle_upload(request)
    except TooLarge as exc:
        raise HTTPException(status_code=413, detail=str(exc))
    except MultipartError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.get("/attachments/{att_id}")
async def download_attachment(att_id: str):
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
# 4. 群状态 group_state
# --------------------------------------------------------------------------


class GroupBody(BaseModel):
    model_config = ConfigDict(extra="ignore")

    group_id: str
    last_msg_ts: int
    group_name: str | None = None


@router.post("/groups")
async def upsert_group_route(body: GroupBody):
    """upsert 群状态。

    响应里带 `previous_last_msg_ts`（更新**前**的值）—— bot 用它做缺口检测，
    省掉"先读再写"那一次竞态。
    """
    return await upsert_group(body.group_id, body.group_name, body.last_msg_ts)


@router.get("/groups")
async def get_groups():
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
async def create_gap_alert(body: GapAlertBody):
    alert_id = await add_gap_alert(
        body.group_id, body.group_name, body.from_ts, body.to_ts, body.reason
    )
    return {"id": alert_id}


@router.get("/gap-alerts")
async def get_gap_alerts(
    acknowledged: str | None = Query(default=None),
    limit: int = Query(default=20, ge=1, le=1000),
):
    rows = await list_gap_alerts(
        acknowledged=_optional_flag(acknowledged, "acknowledged"), limit=limit
    )
    return {"alerts": [{**row, "acknowledged": bool(row.get("acknowledged"))} for row in rows]}


@router.post("/gap-alerts/{alert_id}/ack")
async def ack_gap_alert_route(alert_id: str):
    if not await ack_gap_alert(alert_id):
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
async def create_stats(body: StatsBody):
    increments: dict[str, int] = {}
    for key, value in body.fields.items():
        if key not in STAT_FIELDS:
            continue  # 未知键忽略（契约：未知字段忽略，不报错）
        parsed = as_int(value)
        if parsed is None:
            raise HTTPException(status_code=400, detail=f"{key} 必须是整数")
        increments[key] = parsed
    return await add_stats(body.day, increments)


@router.get("/stats")
async def get_stats(day: str | None = Query(default=None)):
    return await get_stat(day)


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


@router.post("/digest-log")
async def create_digest_log(body: DigestLogBody):
    """记录一次发送（幂等键 `(day, kind, sent)`，见 db.add_digest_log）。

    `kind` 的取值由 bot 定义，后端只存字符串。
    """
    log_id, created = await add_digest_log(
        day=body.day, kind=body.kind, text=body.text, sent=body.sent, error=body.error
    )
    logger.info("digest 记录：id=%s created=%s kind=%s sent=%s", log_id, created, body.kind, body.sent)
    return {"id": log_id}


@router.get("/digest-log")
async def get_digest_log(
    day: str | None = Query(default=None),
    kind: str | None = Query(default=None),
    sent: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=1000),
    count_only: str | None = Query(default=None),
):
    sent_flag = _optional_flag(sent, "sent")
    if _flag(count_only):
        return {"count": await count_digest_logs(day=day, kind=kind, sent=sent_flag)}
    rows = await list_digest_logs(day=day, kind=kind, sent=sent_flag, limit=limit)
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
async def put_state_route(namespace: str, key: str, body: StateBody):
    expires_at = None
    if body.ttl_seconds is not None:
        ttl = as_int(body.ttl_seconds)
        if ttl is None or ttl < 0:
            raise HTTPException(status_code=400, detail="ttl_seconds 必须是非负整数")
        expires_at = now_ms() + ttl * 1000

    await put_state(namespace, key, _json_text(body.value, None), expires_at)
    return {"ok": True, "expires_at": expires_at}


@router.get("/state/{namespace}/{key}")
async def get_state_route(namespace: str, key: str):
    """已过期或不存在一律 404 —— 过期判定在读取时做，不依赖后台清理。"""
    row = await get_state(namespace, key)
    if row is None:
        raise HTTPException(status_code=404, detail="键不存在或已过期")
    return {
        "key": row["key"],
        "value": json_loads(row.get("value"), None),
        "expires_at": row.get("expires_at"),
    }


@router.delete("/state/{namespace}/{key}")
async def delete_state_route(namespace: str, key: str):
    """删除是幂等的：键本来就不存在也返回成功（后置条件都成立）。"""
    await delete_state(namespace, key)
    return {"deleted": True}


@router.get("/state/{namespace}")
async def list_state_route(namespace: str, count_only: str | None = Query(default=None)):
    rows = await list_state(namespace)
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
# 9. 健康：只报存储自身
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
async def health():
    """只报存储自身；QQ 连接、抽取流水线那些状态全在 bot 那边。"""
    settings = get_settings()
    storage_ok = True
    try:
        await fetch_one("SELECT 1 AS ok")
    except Exception:  # pragma: no cover - 只有磁盘/权限坏了才会走到
        logger.exception("存储自检失败")
        storage_ok = False
    writable = _storage_writable()

    return {
        "ok": storage_ok and writable,
        "server_time": now_ms(),
        "storage": {
            "driver": "sqlite",
            "path": str(settings.resolved_db_path),
            "writable": writable,
        },
        "counts": {
            "messages": await count_messages(),
            "notifications": await count_notifications(),
            "attachments": await count_attachments(),
        },
        "version": __version__,
    }
