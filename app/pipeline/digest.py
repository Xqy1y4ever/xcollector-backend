"""每日 digest。

这是任务的**第一出口**：网站需要人主动想起来去看，而 DDL 你没看就没用。
所以 digest 直接推到 QQ 私聊 —— 复用 NapCat 本身，不需要新组件。

尾部固定带一行"盲区"，因为用户必须能区分
"今天确实没通知"和"系统今天瞎了"。
"""

from __future__ import annotations

import asyncio
import logging

from ..config import get_settings
from ..db import (
    digest_auto_attempts_today,
    digest_sent_today,
    fetch_all,
    get_stat,
    list_gap_alerts,
    log_digest,
)
from ..bot_client import get_bot
from ..materialize import build_views
from ..utils import local_day, now_ms, to_local

logger = logging.getLogger(__name__)

MAX_LEN = 1800


def _fmt_due(view: dict) -> str:
    due_at = view.get("due_at")
    text = view.get("due_text") or ""
    conf = view.get("due_confidence") or 0.0

    if due_at is None:
        return f"时间待确认（原文「{text}」）" if text else "无明确时间"

    dt = to_local(due_at)
    stamp = dt.strftime("%m-%d(%a) %H:%M") if dt else "?"

    if conf < 0.6:
        return f"{stamp} 待确认"
    if conf < 0.9:
        return f"~{stamp}"
    return stamp


async def build_digest() -> str:
    settings = get_settings()
    today = local_day()
    now = now_ms()
    day_start = now - 24 * 3600 * 1000

    rows = await fetch_all(
        """SELECT n.*, r.attachments FROM notification n
           LEFT JOIN raw_message r ON r.id = n.raw_message_id
           ORDER BY n.due_at IS NULL, n.due_at ASC"""
    )
    views = await build_views(rows)

    fresh = [v for v in views if (v.get("created_at") or 0) >= day_start and v["status"] != "archived"]
    due_soon = [
        v for v in views
        if v["status"] == "active" and v.get("due_at") and now <= v["due_at"] <= now + 24 * 3600 * 1000
    ]
    expired = [
        v for v in views
        if v["status"] == "expired" and v.get("due_at") and v["due_at"] >= day_start
    ]

    lines: list[str] = [f"【Xcollector 每日通知】{today}", ""]

    lines.append(f"■ 新增 {len(fresh)} 条")
    if not fresh:
        lines.append("  （无）")
    for v in fresh[:12]:
        lines.append(f"· {v['title']}")
        lines.append(f"  截止 {_fmt_due(v)}")
        if v.get("location"):
            lines.append(f"  地点 {v['location']}")
        lines.append(f"  来源 {v.get('group_name') or v.get('group_id')} · {v.get('sender_name') or ''}")
        lines.append(f"  「{v['evidence'][:60]}」")
    if len(fresh) > 12:
        lines.append(f"  …另有 {len(fresh) - 12} 条，请到网站查看")

    if due_soon:
        lines += ["", f"■ 24 小时内到期 {len(due_soon)} 条"]
        for v in due_soon[:8]:
            lines.append(f"· {v['title']} — {_fmt_due(v)}")

    if expired:
        lines += ["", f"■ 已过期 {len(expired)} 条（确认或归档）"]
        for v in expired[:5]:
            lines.append(f"· {v['title']} — {_fmt_due(v)}")

    # ---------------- 盲区 ----------------
    stat = await get_stat(today)
    gaps = await list_gap_alerts(limit=5)

    unparsed = int(stat.get("unparsed") or 0)
    conflicts = int(stat.get("conflicts") or 0)
    degraded = int(stat.get("degraded") or 0)

    lines += ["", "■ 本系统今日盲区"]
    lines.append(
        f"· 收到 {stat.get('ingested', 0)} 条，抽出 {stat.get('extracted', 0)} 条，"
        f"未能解析 {unparsed} 条"
    )
    if conflicts:
        lines.append(f"· {conflicts} 条两个模型给的 DDL 不一致，已标红待确认")
    if degraded:
        lines.append(f"· {degraded} 条在模型失败时降级为规则处理，可能有误")
    if gaps:
        for g in gaps[:3]:
            hours = round((g["to_ts"] - g["from_ts"]) / 3600000, 1)
            lines.append(f"⚠ 群「{g.get('group_name') or g['group_id']}」有 {hours} 小时消息缺口，请手工核对")
    else:
        lines.append("· 未检测到消息缺口")
    if not conflicts and not degraded and not unparsed and not gaps:
        lines.append("· 本日无异常")

    lines += ["", f"（服务器时间 {to_local(now).strftime('%Y-%m-%d %H:%M')}）"]

    text = "\n".join(lines)
    if len(text) > MAX_LEN:
        text = text[: MAX_LEN - 20] + "\n…（已截断，详情见网站）"
    return text


async def send_digest(dry_run: bool = True, kind: str = "manual") -> dict:
    settings = get_settings()
    text = await build_digest()
    today = local_day()
    sent = False
    error: str | None = None

    if dry_run:
        await log_digest(today, "preview", text, sent=False)
        return {"ok": True, "sent": False, "text": text, "error": None}

    if not settings.digest_target_qq:
        error = "未配置 DIGEST_TARGET_QQ，无法发送"
    else:
        # bot 不可达不抛异常，返回 (ok, error)，这样调度循环还能继续跑
        sent, error = await get_bot().send_private(settings.digest_target_qq, text)
        if not sent and not error:
            error = "bot 报告发送失败，但没给出原因"

    await log_digest(today, kind, text, sent=sent, error=error)
    return {"ok": sent, "sent": sent, "text": text, "error": error}


MAX_AUTO_ATTEMPTS = 3


async def digest_loop() -> None:
    """每分钟检查一次是否到了发送时间。"""
    settings = get_settings()
    if not settings.digest_enabled:
        logger.info("每日 digest 未启用")
        return

    hh, mm = settings.digest_hhmm
    logger.info("每日 digest 已启用：%02d:%02d（%s）", hh, mm, settings.digest_tz)

    while True:
        try:
            now = to_local(now_ms())
            today = local_day()
            if now and (now.hour, now.minute) >= (hh, mm):
                if not await digest_sent_today(today):
                    attempts = await digest_auto_attempts_today(today)
                    if attempts >= MAX_AUTO_ATTEMPTS:
                        if attempts == MAX_AUTO_ATTEMPTS:
                            logger.error(
                                "每日 digest 今日已失败 %d 次，不再重试；请检查 DIGEST_TARGET_QQ 与 OneBot 连接",
                                attempts,
                            )
                    else:
                        result = await send_digest(dry_run=False, kind="auto")
                        if result["sent"]:
                            logger.info("每日 digest 已发送")
                        else:
                            logger.warning("每日 digest 发送失败：%s", result.get("error"))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("digest 循环异常：%s", exc)
        await asyncio.sleep(60)
