"""注入演示数据，用于在没有 NapCat、没有 API key 的情况下验证整条链路。

    python -m app.tools.seed_demo --reset              # 用配置里的 EXTRACTOR
    python -m app.tools.seed_demo --reset --extractor rule
    python -m app.tools.seed_demo --reset --extractor llm

演示数据的 message_id 都以 `seed-` 开头，`--reset` 只会删掉这些行，不碰真实数据。
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time

from ..config import get_settings
from ..db import close_db, execute, init_db
from ..pipeline.ingest import handle_event, close_http
from ..utils import local_day

# (文本, 发送者QQ, 发送者昵称, 多少分钟前, 是否@全体成员)
SAMPLES: list[tuple[str, str, str, int, bool]] = [
    ("大家下周三前把军训心得交到班长那里，不少于800字，电子版发我邮箱。", "10001", "李老师", 200, True),
    ("请各位同学于9月20日24:00前完成本学期选课确认，逾期系统自动关闭。", "10002", "王导", 180, True),
    ("明天中午12点前把体检表交到学工办，过时不候。", "10001", "李老师", 150, False),
    ("收到", "20001", "同学甲", 145, False),
    ("本周五19:00在教三201开班会，请全体同学准时参加。", "10002", "王导", 120, True),
    ("关于奖学金评定，后续安排请关注群通知。", "10002", "王导", 90, False),
    ("哈哈哈哈", "20002", "同学乙", 80, False),
    ("【重要】下周一之前每个人必须完成安全教育平台的课程学习，没完成的会影响评优。", "10001", "李老师", 60, True),
    ("有没有人一起拼单奶茶", "20003", "同学丙", 40, False),
    ("请各班班长统计一下本班参加运动会的人数，3天内报给我。", "10002", "王导", 20, True),
]


async def main() -> int:
    parser = argparse.ArgumentParser(description="注入 Xcollector 演示数据")
    parser.add_argument("--reset", action="store_true", help="先删除上次注入的演示数据")
    parser.add_argument(
        "--extractor",
        choices=["rule", "llm", "both"],
        default=None,
        help="临时覆盖 EXTRACTOR 配置（默认用 .env 里的值）",
    )
    args = parser.parse_args()

    settings = get_settings()
    if args.extractor:
        settings.extractor = args.extractor

    await init_db()

    if args.reset:
        await execute(
            "DELETE FROM notification WHERE raw_message_id IN (SELECT id FROM raw_message WHERE message_id LIKE 'seed-%')"
        )
        n = await execute("DELETE FROM raw_message WHERE message_id LIKE 'seed-%'")
        # 当日统计必须一起清掉，否则 digest 里的"收到 N 条"会和实际演示数据对不上。
        # 盲区数字一旦不可信，整个系统就没有可信的部分了。
        today = local_day()
        await execute("DELETE FROM digest_log WHERE day=?", (today,))
        await execute("DELETE FROM pipeline_stat WHERE day=?", (today,))
        print(f"已清除 {n} 条旧的演示原始消息，并重置当日统计")

    groups = list(settings.group_whitelist_map.keys())
    group_id = groups[0] if groups else "123456789"
    senders = list(settings.sender_whitelist_map.keys())

    print(f"使用群 {group_id}，抽取模式 {settings.extractor}")
    if groups and not senders and settings.sender_whitelist_mode == "strict":
        print("提示：SENDER_WHITELIST 为空，strict 模式下不会过滤任何发送者")

    now = int(time.time())
    injected = 0
    for idx, (text, sender_id, sender_name, minutes_ago, at_all) in enumerate(SAMPLES, start=1):
        if senders and sender_id not in senders and settings.sender_whitelist_mode == "strict":
            # 让演示数据至少有一部分能通过白名单，方便观察 skipped_whitelist 状态
            pass
        message: list[dict] = []
        if at_all:
            message.append({"type": "at", "data": {"qq": "all", "name": ""}})
        message.append({"type": "text", "data": {"text": (" " if at_all else "") + text}})

        event = {
            "post_type": "message",
            "message_type": "group",
            "sub_type": "normal",
            "self_id": "999999",
            "group_id": group_id,
            "user_id": sender_id,
            "message_id": f"seed-{idx:02d}",
            "time": now - minutes_ago * 60,
            "raw_message": text,
            "sender": {"user_id": sender_id, "nickname": sender_name, "card": sender_name},
            "message": message,
        }
        await handle_event(event)
        injected += 1

    print(f"已注入 {injected} 条演示消息")

    from ..db import fetch_all

    stats = await fetch_all("SELECT state, COUNT(*) AS c FROM raw_message GROUP BY state")
    notifs = await fetch_all("SELECT COUNT(*) AS c FROM notification")
    print("原始消息状态分布：", {r["state"]: r["c"] for r in stats})
    print("通知条目数：", notifs[0]["c"] if notifs else 0)

    await close_http()
    await close_db()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
