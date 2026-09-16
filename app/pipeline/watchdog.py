"""缺口 / 静默看门狗。

NapCat 靠实时事件推送，历史消息拉取能力有限且不稳定。
这意味着 **bot 挂掉的那几个小时里的官方通知会永久消失**，而重启后一切看起来正常。

既然"官方通知恰好就是不能漏的那些"，系统必须主动承认自己的盲区，
而不是安静地继续工作。
"""

from __future__ import annotations

import asyncio
import logging

from ..config import get_settings
from ..db import add_gap_alert, fetch_one, list_group_states
from ..utils import now_ms

logger = logging.getLogger(__name__)

DEDUPE_HOURS = 6
CHECK_INTERVAL = 15 * 60


async def _recent_alert_exists(group_id: str) -> bool:
    row = await fetch_one(
        """SELECT id FROM gap_alert
           WHERE group_id=? AND acknowledged=0 AND created_at > ?
           LIMIT 1""",
        (str(group_id), now_ms() - DEDUPE_HOURS * 3600 * 1000),
    )
    return row is not None


async def check_silence(reason: str = "silence") -> int:
    """检查白名单群是否长时间没有消息。返回新增告警数。"""
    settings = get_settings()
    threshold_ms = settings.gap_alert_hours * 3600 * 1000
    now = now_ms()
    created = 0

    states = {str(s["group_id"]): s for s in await list_group_states()}

    # 白名单里有、但从未收到过消息的群，也提示一下（可能群号配错了）
    for gid, name in settings.group_whitelist_map.items():
        state = states.get(gid)
        if state is None:
            continue
        last_at = state.get("last_msg_at")
        if not last_at:
            continue
        silent_ms = now - int(last_at)
        if silent_ms <= threshold_ms:
            continue
        if await _recent_alert_exists(gid):
            continue
        await add_gap_alert(
            gid,
            state.get("group_name") or name,
            int(state.get("last_msg_ts") or 0),
            now,
            reason=(
                f"[{reason}] 已 {round(silent_ms / 3600000, 1)} 小时未收到该群任何消息，"
                "可能是连接中断或 NapCat 掉线，此期间的通知可能已永久丢失"
            ),
        )
        created += 1
        logger.warning("群 %s 静默 %.1f 小时，已生成缺口告警", gid, silent_ms / 3600000)

    return created


async def startup_gap_check() -> None:
    n = await check_silence(reason="startup")
    if n:
        logger.warning("启动检查发现 %d 个群存在消息缺口", n)
    else:
        logger.info("启动检查：未发现消息缺口")


async def silence_loop() -> None:
    await asyncio.sleep(30)
    while True:
        try:
            await check_silence(reason="periodic")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("静默检查异常：%s", exc)
        await asyncio.sleep(CHECK_INTERVAL)
