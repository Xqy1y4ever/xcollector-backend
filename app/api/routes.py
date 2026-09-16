"""HTTP API。

前端只需要这些接口：列表 / 详情 / 修正 / 已读 / 健康 / digest。
"""

from __future__ import annotations

import json
import logging

from fastapi import APIRouter, Header, HTTPException, Query
from pydantic import BaseModel

from ..bot_client import get_bot
from ..config import get_settings
from ..db import (
    add_correction,
    fetch_all,
    fetch_one,
    get_stat,
    list_gap_alerts,
    list_group_states,
    set_read,
)
from ..materialize import build_view, build_views
from ..pipeline.digest import build_digest, send_digest
from ..pipeline.ingest import handle_messages
from ..pipeline.manual import create_manual_task
from ..utils import local_day, now_ms

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api")

UNPARSED_WINDOW_DAYS = 7
CORRECTABLE_FIELDS = {"title", "summary", "location", "due_at", "due_text", "status"}
# done 与 archived 的区别：done = 做完了，archived = 不是通知/误报。
# 前端把 done 单独成组弱化显示，archived 直接从主列表消失。
VALID_STATUS = {"active", "archived", "done"}


# --------------------------------------------------------------------------
# 盲区：必须能回答"今天是不是系统瞎了"
# --------------------------------------------------------------------------


async def _blindspots() -> dict:
    settings = get_settings()
    cutoff = now_ms() - UNPARSED_WINDOW_DAYS * 24 * 3600 * 1000

    unparsed = await fetch_one(
        "SELECT COUNT(*) AS c FROM raw_message WHERE state IN ('unparsed','degraded') AND ts >= ?",
        (cutoff,),
    )
    conflicts = await fetch_one("SELECT COUNT(*) AS c FROM notification WHERE conflict=1")
    low_conf = await fetch_one(
        "SELECT COUNT(*) AS c FROM notification WHERE due_confidence < ? AND due_confidence > 0",
        (settings.low_confidence_threshold,),
    )
    stat = await get_stat()
    gaps = await list_gap_alerts(limit=20)

    return {
        "unparsed_count": int((unparsed or {}).get("c") or 0),
        "conflict_count": int((conflicts or {}).get("c") or 0),
        "low_confidence_count": int((low_conf or {}).get("c") or 0),
        "gap_alerts": gaps,
        "degraded_today": int(stat.get("degraded") or 0) > 0,
        "window_days": UNPARSED_WINDOW_DAYS,
    }


# --------------------------------------------------------------------------
# 通知列表 / 详情
# --------------------------------------------------------------------------


@router.get("/notifications")
async def list_notifications(
    since: int | None = Query(default=None, description="只返回 updated_at > since 的条目（毫秒）"),
    status: str = Query(default="all"),
    q: str | None = Query(default=None),
    limit: int = Query(default=500, ge=1, le=2000),
):
    sql = """SELECT n.*, r.attachments FROM notification n
             LEFT JOIN raw_message r ON r.id = n.raw_message_id"""
    params: list = []
    if since:
        sql += " WHERE n.updated_at > ?"
        params.append(since)
    sql += " ORDER BY n.due_at IS NULL, n.due_at ASC, n.source_ts DESC LIMIT ?"
    params.append(limit)

    rows = await fetch_all(sql, params)
    views = await build_views(rows)

    if status and status != "all":
        views = [v for v in views if v["status"] == status]

    if q:
        needle = q.strip().lower()
        views = [
            v
            for v in views
            if needle in (v.get("title") or "").lower()
            or needle in (v.get("summary") or "").lower()
            or needle in (v.get("evidence") or "").lower()
        ]

    return {
        "server_time": now_ms(),
        "notifications": views,
        "blindspots": await _blindspots(),
    }


@router.get("/notifications/{notif_id}")
async def get_notification_detail(notif_id: str):
    row = await fetch_one(
        """SELECT n.*, r.attachments FROM notification n
           LEFT JOIN raw_message r ON r.id = n.raw_message_id
           WHERE n.id = ?""",
        (notif_id,),
    )
    if row is None:
        raise HTTPException(status_code=404, detail="通知不存在")

    raw = await fetch_one("SELECT * FROM raw_message WHERE id=?", (row["raw_message_id"],))
    view = await build_view(row)
    view.pop("_read", None)

    raw_view = None
    if raw:
        try:
            attachments = json.loads(raw.get("attachments") or "[]")
        except json.JSONDecodeError:
            attachments = []
        raw_view = {
            "id": raw["id"],
            "group_id": raw["group_id"],
            "group_name": raw["group_name"],
            "sender_id": raw["sender_id"],
            "sender_name": raw["sender_name"],
            "ts": raw["ts"],
            "content": raw["content"],
            "attachments": attachments,
            "raw_json": raw["raw"],
            "state": raw.get("state"),
            "state_reason": raw.get("state_reason"),
        }
    return {"notification": view, "raw": raw_view}


# --------------------------------------------------------------------------
# 人工修正 / 已读
# --------------------------------------------------------------------------


class CorrectionBody(BaseModel):
    field: str
    value: object | None = None
    user_id: str = "web"


@router.post("/notifications/{notif_id}/corrections")
async def create_correction(notif_id: str, body: CorrectionBody):
    if body.field not in CORRECTABLE_FIELDS:
        raise HTTPException(status_code=400, detail=f"不可修正的字段：{body.field}")

    row = await fetch_one("SELECT id FROM notification WHERE id=?", (notif_id,))
    if row is None:
        raise HTTPException(status_code=404, detail="通知不存在")

    value = body.value
    if body.field == "due_at":
        if value in (None, "", "null"):
            value = None
        else:
            try:
                value = int(float(value))
            except (TypeError, ValueError):
                raise HTTPException(status_code=400, detail="due_at 必须是毫秒时间戳或 null")
    elif body.field == "status":
        if value not in VALID_STATUS:
            raise HTTPException(status_code=400, detail=f"status 只能是 {sorted(VALID_STATUS)}")
    else:
        value = "" if value is None else str(value)

    await add_correction(notif_id, body.field, value, body.user_id)

    full = await fetch_one(
        """SELECT n.*, r.attachments FROM notification n
           LEFT JOIN raw_message r ON r.id = n.raw_message_id WHERE n.id=?""",
        (notif_id,),
    )
    view = await build_view(full)
    view.pop("_read", None)
    return {"ok": True, "notification": view}


class ReadBody(BaseModel):
    read: bool = True


@router.post("/notifications/{notif_id}/read")
async def mark_read(notif_id: str, body: ReadBody):
    row = await fetch_one("SELECT id FROM notification WHERE id=?", (notif_id,))
    if row is None:
        raise HTTPException(status_code=404, detail="通知不存在")
    await set_read(notif_id, body.read)
    return {"ok": True, "read": body.read}


# --------------------------------------------------------------------------
# 健康 / 盲区
# --------------------------------------------------------------------------


@router.get("/health")
async def health():
    settings = get_settings()
    bot_status = await get_bot().status()
    now = now_ms()

    states = {str(s["group_id"]): s for s in await list_group_states()}
    groups: list[dict] = []
    for gid, name in settings.group_display_names.items():
        s = states.pop(gid, None)
        if s is None:
            groups.append(
                {
                    "group_id": gid,
                    "group_name": name,
                    "in_whitelist": True,
                    "last_msg_ts": None,
                    "last_msg_at": None,
                    "silent_hours": None,
                    "msg_count_today": 0,
                    "note": "尚未收到该群任何消息：群号可能配错，或 bot 未订阅该群",
                }
            )
            continue
        last_at = s.get("last_msg_at")
        groups.append(
            {
                "group_id": gid,
                "group_name": s.get("group_name") or name,
                "in_whitelist": True,
                "last_msg_ts": s.get("last_msg_ts"),
                "last_msg_at": last_at,
                "silent_hours": round((now - int(last_at)) / 3600000, 2) if last_at else None,
                "msg_count_today": int(s.get("msg_count_today") or 0),
            }
        )
    # 不在白名单但收到过消息的群（配置改了之后的老数据）
    for gid, s in states.items():
        groups.append(
            {
                "group_id": gid,
                "group_name": s.get("group_name"),
                "in_whitelist": False,
                "last_msg_ts": s.get("last_msg_ts"),
                "last_msg_at": s.get("last_msg_at"),
                "silent_hours": None,
                "msg_count_today": int(s.get("msg_count_today") or 0),
            }
        )

    stat = await get_stat()
    return {
        "server_time": now,
        # 键名保留 onebot 以兼容前端；内容是 bot 转发过来的连接状态
        "onebot": bot_status,
        "bot": bot_status,
        "groups": groups,
        "pipeline": {
            "today_ingested": int(stat.get("ingested") or 0),
            "today_extracted": int(stat.get("extracted") or 0),
            "today_unparsed": int(stat.get("unparsed") or 0),
            "today_conflicts": int(stat.get("conflicts") or 0),
            "today_degraded": int(stat.get("degraded") or 0),
            "today_llm_tokens": int(stat.get("llm_tokens") or 0),
        },
        "llm": {
            "enabled": settings.extractor in ("llm", "both"),
            "extractor": settings.extractor,
            "primary_model": settings.llm_primary_model,
            "secondary_model": settings.llm_secondary_model or None,
            "cross_check_enabled": settings.cross_check_enabled,
            "vlm_enabled": settings.vlm_enabled,
        },
        "gap_alerts": await list_gap_alerts(limit=20),
        "blindspots": await _blindspots(),
        "day": local_day(),
    }


# --------------------------------------------------------------------------
# digest
# --------------------------------------------------------------------------


@router.get("/digest/preview")
async def digest_preview():
    return {"text": await build_digest()}


class DigestSendBody(BaseModel):
    dry_run: bool = True


@router.post("/digest/send")
async def digest_send(body: DigestSendBody):
    return await send_digest(dry_run=body.dry_run, kind="manual")


# --------------------------------------------------------------------------
# 配置摘要（不含密钥）
# --------------------------------------------------------------------------


@router.get("/config/meta")
async def config_meta():
    settings = get_settings()
    return {
        "group_whitelist": [
            {"group_id": k, "name": v} for k, v in settings.group_whitelist_map.items()
        ],
        "sender_whitelist": [
            {"sender_id": k, "name": v} for k, v in settings.sender_whitelist_map.items()
        ],
        "sender_whitelist_mode": settings.sender_whitelist_mode,
        "digest_time": settings.digest_time,
        "digest_enabled": settings.digest_enabled,
        "digest_target_qq": settings.digest_target_qq or None,
        "extractor": settings.extractor,
        "bot_base_url": settings.bot_base_url,
        "bot_token_configured": bool(settings.bot_api_token),
        "ingest_token_configured": bool(settings.ingest_api_token),
        "low_confidence_threshold": settings.low_confidence_threshold,
        "gap_alert_hours": settings.gap_alert_hours,
    }


# --------------------------------------------------------------------------
# 接入：bot 推消息进来
# --------------------------------------------------------------------------


def _check_ingest_token(authorization: str | None) -> None:
    settings = get_settings()
    expected = settings.ingest_api_token
    if not expected:
        return  # 未配置令牌 = 不校验（仅本地开发）
    if (authorization or "") != f"Bearer {expected}":
        raise HTTPException(status_code=401, detail="ingest 令牌无效")


class IngestBody(BaseModel):
    messages: list[dict] = []


@router.post("/ingest/messages")
async def ingest_messages(body: IngestBody, authorization: str | None = Header(default=None)):
    """bot 把归一化后的 QQ 消息推到这里。

    注意：这一层是**推送入口**，不是查询接口。它对每条消息做同样的事：
    落原始层 → 群/发送者白名单 → 抽取。任何消息都不会被静默丢弃，
    白名单外/重复的消息也会在日志里留下记录。
    """
    _check_ingest_token(authorization)
    if not body.messages:
        return {"ok": True, "received": 0}
    return await handle_messages(body.messages)


# --------------------------------------------------------------------------
# 手动建任务（QQ 里用 /add 指令）
# --------------------------------------------------------------------------


class ManualTaskBody(BaseModel):
    text: str
    sender_id: str = ""
    sender_name: str = ""
    # auto_commit: 解析有把握就直接建；没把握则只返回 preview 让 bot 回问
    auto_commit: bool = True
    # force_commit: 用户在 bot 里确认过了，无论有没有把握都建
    force_commit: bool = False


@router.post("/tasks/manual")
async def create_manual(body: ManualTaskBody):
    if not body.text.strip():
        raise HTTPException(status_code=400, detail="text 不能为空")
    return await create_manual_task(
        text=body.text,
        sender_id=body.sender_id,
        sender_name=body.sender_name,
        auto_commit=body.auto_commit,
        force_commit=body.force_commit,
    )
