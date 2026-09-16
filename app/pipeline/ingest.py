"""归一化消息接入：bot 推过来的消息 → 原始层。

职责边界（很重要）：
  这一层**只负责把消息无损搬进来**，不做任何"有效性判断"。
  任何消息都不在这里被丢弃 —— 唯一例外是"不在群白名单里"（那是用户显式配置的边界）。

拆分之后这一层不认识 OneBot 协议：合并转发已由 bot 展开成纯文本，
我们收到的就是 `{group_id, sender_id, ts, text, attachments, ...}`。
"""

from __future__ import annotations

import logging
import mimetypes
import re
import time
from pathlib import Path
from typing import Any, Iterable

import httpx

from ..config import get_settings
from ..db import add_gap_alert, insert_raw_message, set_raw_state, touch_group
from .runner import process_raw
from .trace import log_message, message_meta

logger = logging.getLogger(__name__)

_http: httpx.AsyncClient | None = None
_SAFE = re.compile(r"[^0-9A-Za-z_.-]")


def _client() -> httpx.AsyncClient:
    global _http
    if _http is None:
        _http = httpx.AsyncClient(timeout=30.0, follow_redirects=True)
    return _http


async def close_http() -> None:
    global _http
    if _http is not None:
        await _http.aclose()
        _http = None


# --------------------------------------------------------------------------
# 附件落地
# --------------------------------------------------------------------------


def _guess_ext(att: dict, content_type: str | None) -> str:
    if content_type:
        ext = mimetypes.guess_extension(content_type.split(";")[0].strip())
        if ext:
            return ext
    name = att.get("name")
    if name and "." in name:
        return "." + name.rsplit(".", 1)[1][:8]
    url = att.get("url")
    if url:
        suffix = Path(url.split("?")[0]).suffix
        if suffix and len(suffix) <= 8:
            return suffix
    return ".bin"


async def _download_attachments(
    group_id: str, message_id: str, attachments: list[dict]
) -> None:
    """把附件下载到本地（就地写入 `local_path` / `download_error`）。

    bot 那边只透传 URL，因为文件必须保存在**后端**这一侧，
    否则前后端分开部署时前端就取不到图了。
    而 URL 有时效性，过期就再也取不回来 —— 所以落地是硬要求，
    下载失败也要在附件上留下明确痕迹，而不是悄悄跳过。
    """
    settings = get_settings()
    if not settings.media_download_enabled or not attachments:
        return

    out_dir = settings.resolved_attachment_dir
    out_dir.mkdir(parents=True, exist_ok=True)

    for idx, att in enumerate(attachments):
        url = att.get("url")
        if not url:
            att["download_error"] = "该附件没有可用 URL（bot 未提供）"
            continue
        try:
            resp = await _client().get(url)
            resp.raise_for_status()
            body = resp.content
            if len(body) > settings.media_max_bytes:
                att["download_error"] = f"附件超过大小上限 ({len(body)} bytes)"
                continue
            ext = _guess_ext(att, resp.headers.get("content-type"))
            stem = _SAFE.sub("_", f"{group_id}_{message_id}_{idx}")
            path = out_dir / f"{stem}{ext}"
            path.write_bytes(body)
            att["local_path"] = str(
                path.relative_to(settings.resolved_attachment_dir.parent.parent)
            )
        except Exception as exc:
            att["download_error"] = f"{type(exc).__name__}: {exc}"
            logger.warning("附件下载失败 %s: %s", str(url)[:120], exc)


# --------------------------------------------------------------------------
# 主入口
# --------------------------------------------------------------------------


async def handle_message(message: dict) -> str:
    """处理一条归一化消息。返回粗粒度的结局，供 API 统计。

    结局：accepted | duplicate | group_filtered | skipped_whitelist | error
    （更细的结局 extracted/noise/unparsed/degraded 由 runner 记在日志里）
    """
    settings = get_settings()

    group_id = str(message.get("group_id") or "")
    sender_id = str(message.get("sender_id") or "")
    message_id = str(message.get("message_id") or "")

    if not group_id or not message_id:
        return "error"

    ts = int(message.get("ts") or 0) or int(time.time() * 1000)
    content = message.get("text") or ""

    meta = message_meta(message, content=content)
    meta["ts"] = ts

    if not settings.in_group_whitelist(group_id):
        # 白名单之外的群连库都不进。这类量可能很大，所以只记 DEBUG。
        log_message(meta, "group_filtered", 原因="群不在白名单")
        return "group_filtered"

    try:
        attachments: list[dict] = list(message.get("attachments") or [])
        await _download_attachments(group_id, message_id, attachments)

        raw_id, is_new = await insert_raw_message(
            message_id=message_id,
            group_id=group_id,
            group_name=message.get("group_name"),
            sender_id=sender_id,
            sender_name=message.get("sender_name") or sender_id,
            ts=ts,
            content=content,
            attachments=attachments,
            # 原始层的 raw 列留档整条归一化消息（含 bot 附上的原始事件）
            raw=message,
        )
        if not is_new:
            log_message(meta, "duplicate")  # 重连后重复推送，只记 DEBUG
            return "duplicate"

        gap = await touch_group(group_id, message.get("group_name"), ts)
        if gap:
            await add_gap_alert(
                gap["group_id"],
                gap["group_name"],
                gap["from_ts"],
                gap["to_ts"],
                reason=(
                    f"两条消息间隔 {round((gap['to_ts'] - gap['from_ts']) / 3600000, 1)} 小时，"
                    "可能有消息在断线期间丢失，请手工核对"
                ),
            )
            logger.warning("检测到消息缺口：群 %s", group_id)

        # 发送者白名单：名单外的消息只入库、不抽取
        if not settings.in_sender_whitelist(sender_id):
            await set_raw_state(raw_id, "skipped_whitelist", f"发送者 {sender_id} 不在白名单")
            log_message(meta, "skipped_whitelist", 原因=f"发送者 {sender_id} 不在白名单")
            return "skipped_whitelist"

        # 最终结果由 runner 记录（extracted / noise / unparsed / degraded）
        await process_raw(raw_id)
        return "accepted"

    except Exception as exc:
        # 兜底：任何未预期的异常也要留下这一条消息的记录，不能让它在日志里消失
        log_message(meta, "error", 原因=f"{type(exc).__name__}: {exc}")
        logger.exception("消息处理失败 msg_id=%s", message_id)
        return "error"


async def handle_messages(messages: Iterable[dict]) -> dict[str, Any]:
    """批量入口。逐条处理，一条失败不影响其余。"""
    counters: dict[str, int] = {}
    for message in messages:
        try:
            outcome = await handle_message(message)
        except Exception as exc:  # handle_message 内部已兜底，这里是双保险
            logger.exception("批量接入时单条失败：%s", exc)
            outcome = "error"
        counters[outcome] = counters.get(outcome, 0) + 1

    return {
        "ok": True,
        "received": sum(counters.values()),
        "accepted": counters.get("accepted", 0),
        "duplicates": counters.get("duplicate", 0),
        "filtered": counters.get("group_filtered", 0),
        "skipped": counters.get("skipped_whitelist", 0),
        "errors": counters.get("error", 0),
        "detail": counters,
    }
