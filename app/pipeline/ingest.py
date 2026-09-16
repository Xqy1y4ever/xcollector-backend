"""事件接入：OneBot 事件 → 原始层。

职责边界（很重要）：
  这一层**只负责把消息无损搬进来**，不做任何"有效性判断"。
  任何消息都不在这里被丢弃 —— 唯一例外是"不在群白名单里"（那是用户显式配置的边界）。
"""

from __future__ import annotations

import logging
import mimetypes
import re
import time
from pathlib import Path

import httpx

from ..config import get_settings
from ..db import add_gap_alert, insert_raw_message, set_raw_state, touch_group
from ..onebot import get_hub, parse_message
from ..onebot.segments import Attachment
from .runner import process_raw
from .trace import event_meta, log_message

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
# 合并转发展开
# --------------------------------------------------------------------------


async def _expand_forwards(
    parsed_text: str, forward_ids: list[str], depth: int, seen: set[str]
) -> str:
    """递归展开合并转发。

    NapCat 对合并转发的消息体是空的，只有 id。不展开的话这条通知就没了。
    """
    settings = get_settings()
    if not forward_ids or depth >= settings.forward_max_depth:
        return parsed_text

    hub = get_hub()
    chunks: list[str] = [parsed_text] if parsed_text.strip() else []
    for fid in forward_ids:
        if fid in seen:
            continue
        seen.add(fid)
        try:
            nodes = await hub.get_forward_msg(fid)
        except Exception as exc:
            logger.warning("展开合并转发失败 id=%s: %s", fid, exc)
            chunks.append(f"[合并转发展开失败: {fid}]")
            continue

        lines: list[str] = []
        for node in nodes:
            inner = parse_message(node.get("message") or node.get("content") or [])
            sender = (node.get("sender") or {}).get("nickname") or ""
            nested = await _expand_forwards(
                inner.text, inner.forwards, depth + 1, seen
            )
            lines.append(f"  <{sender}> {nested}")
        if lines:
            chunks.append("[合并转发内容]\n" + "\n".join(lines))
    return "\n".join(c for c in chunks if c.strip())


# --------------------------------------------------------------------------
# 附件落地
# --------------------------------------------------------------------------


def _guess_ext(att: Attachment, content_type: str | None) -> str:
    if content_type:
        ext = mimetypes.guess_extension(content_type.split(";")[0].strip())
        if ext:
            return ext
    if att.name and "." in att.name:
        return "." + att.name.rsplit(".", 1)[1][:8]
    if att.url:
        suffix = Path(att.url.split("?")[0]).suffix
        if suffix and len(suffix) <= 8:
            return suffix
    return ".bin"


async def _download_attachments(
    group_id: str, message_id: str, attachments: list[Attachment]
) -> None:
    """把附件下载到本地。

    NapCat 给的 URL 有时效性，过期就再也取不回来了 ——
    所以"落地保存"是硬要求，下载失败也要留下明确痕迹。
    """
    settings = get_settings()
    if not settings.media_download_enabled or not attachments:
        return

    out_dir = settings.resolved_attachment_dir
    out_dir.mkdir(parents=True, exist_ok=True)

    for idx, att in enumerate(attachments):
        if not att.url:
            att.download_error = "该附件没有可用 URL（OneBot 未提供）"
            continue
        try:
            resp = await _client().get(att.url)
            resp.raise_for_status()
            body = resp.content
            if len(body) > settings.media_max_bytes:
                att.download_error = f"附件超过大小上限 ({len(body)} bytes)"
                continue
            ext = _guess_ext(att, resp.headers.get("content-type"))
            stem = _SAFE.sub("_", f"{group_id}_{message_id}_{idx}")
            path = out_dir / f"{stem}{ext}"
            path.write_bytes(body)
            att.local_path = str(path.relative_to(settings.resolved_attachment_dir.parent.parent))
        except Exception as exc:
            att.download_error = f"{type(exc).__name__}: {exc}"
            logger.warning("附件下载失败 %s: %s", att.url[:120], exc)


# --------------------------------------------------------------------------
# 主入口
# --------------------------------------------------------------------------


async def handle_event(event: dict) -> None:
    settings = get_settings()

    if event.get("post_type") != "message":
        return
    if event.get("message_type") != "group":
        return  # MVP 只处理群消息
    if str(event.get("self_id")) == str(event.get("user_id")):
        return  # 机器人自己发的消息

    group_id = str(event.get("group_id"))
    sender_id = str(event.get("user_id"))

    ts = int(event.get("time", 0)) * 1000 or int(time.time() * 1000)
    parsed = parse_message(event.get("message"))

    # 日志上下文：即使这条消息最终不入库（群不在白名单），也要能记一行
    meta = event_meta(event, content=parsed.text, parsed=parsed)
    meta["ts"] = ts

    if not settings.in_group_whitelist(group_id):
        # 白名单之外的群连库都不进。这类量可能很大，所以只记 DEBUG。
        log_message(meta, "group_filtered", 原因="群不在白名单")
        return

    try:
        sender = event.get("sender") or {}
        meta["sender_name"] = sender.get("card") or sender.get("nickname") or sender_id

        group_name = settings.group_whitelist_map.get(group_id)
        if group_name == group_id or group_name is None:
            group_name = await _try_group_name(group_id)
        meta["group_name"] = group_name

        content = await _expand_forwards(parsed.text, parsed.forwards, 0, set())
        meta["content"] = content

        await _download_attachments(group_id, str(event.get("message_id")), parsed.attachments)

        raw_id, is_new = await insert_raw_message(
            message_id=str(event.get("message_id")),
            group_id=group_id,
            group_name=group_name,
            sender_id=sender_id,
            sender_name=meta["sender_name"],
            ts=ts,
            content=content,
            attachments=[a.to_dict() for a in parsed.attachments],
            raw=event,
        )
        if not is_new:
            log_message(meta, "duplicate")  # 重连后重复推送，只记 DEBUG
            return

        gap = await touch_group(group_id, group_name, ts)
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
            return

        # 最终结果由 runner 记录（extracted / noise / unparsed / degraded）
        await process_raw(raw_id)

    except Exception as exc:
        # 兜底：任何未预期的异常也要留下这一条消息的记录，不能让它在日志里消失
        log_message(meta, "error", 原因=f"{type(exc).__name__}: {exc}")
        raise


async def _try_group_name(group_id: str) -> str | None:
    try:
        info = await get_hub().get_group_info(group_id)
        return info.get("group_name")
    except Exception:
        return None
