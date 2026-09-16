"""验证「每条被处理的消息恰好产生一行日志」。

    python -m tests.check_message_log

日志是这个系统的排障入口：如果一条消息进来之后日志里没有它，
那它要么被静默丢弃了，要么流程断在了某处 —— 两种都不能接受。
所以这里对每种结局都断言"有且只有一行"。
"""

from __future__ import annotations

import asyncio
import logging
import re
import sys
import time
from typing import Callable

from app.config import get_settings
from app.db import close_db, execute, init_db
from app.pipeline.ingest import close_http, handle_message

MSG_ID = re.compile(r"msg_id=(\S+)")
OUTCOME = re.compile(r"结果=(\S+)")

GROUP = "123456789"
OTHER_GROUP = "999999"


class Capture(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(record.getMessage())


def _event(message_id: str, group_id: str, text: str, sender_id: str, sender_name: str) -> dict:
    """构造一条 bot 会推过来的**归一化消息**（不是 OneBot 原始事件）。"""
    now = int(time.time())
    return {
        "source": "qq",
        "message_id": message_id,
        "group_id": group_id,
        "group_name": None,
        "sender_id": sender_id,
        "sender_name": sender_name,
        "ts": (now - 60) * 1000,
        "text": text,
        "at_all": False,
        "mentions": [],
        "reply_to": None,
        "attachments": [],
        "raw": {},
    }


async def _run() -> int:
    settings = get_settings()

    # 这条测试要能独立断言，所以不依赖 .env 的内容：
    # 显式清空白名单并强制用规则抽取（避免真的去调 LLM）。
    saved = (
        settings.extractor,
        settings.group_whitelist,
        settings.sender_whitelist,
        settings.sender_whitelist_mode,
    )
    settings.extractor = "rule"
    settings.group_whitelist = ""
    settings.sender_whitelist = ""
    settings.sender_whitelist_mode = "off"

    handler = Capture()
    msg_logger = logging.getLogger("xcollector.message")
    msg_logger.addHandler(handler)
    msg_logger.setLevel(logging.DEBUG)
    msg_logger.propagate = False

    await init_db()
    await _cleanup()

    def sender_wl(value: str, mode: str) -> Callable[[], None]:
        def apply() -> None:
            settings.sender_whitelist = value
            settings.sender_whitelist_mode = mode

        return apply

    def group_wl(value: str) -> Callable[[], None]:
        def apply() -> None:
            settings.group_whitelist = value

        return apply

    # (用例名, 事件, 期望的日志结果, 执行前, 执行后)
    cases: list[tuple[str, dict, str, Callable[[], None] | None, Callable[[], None] | None]] = [
        (
            "notice",
            _event("logchk-1", GROUP, "请大家下周三前提交军训心得，不少于800字。", "10001", "李老师"),
            "extracted",
            None,
            None,
        ),
        (
            "chatter",
            _event("logchk-2", GROUP, "收到", "20001", "同学甲"),
            "noise",
            None,
            None,
        ),
        (
            "stranger",
            _event("logchk-3", GROUP, "老师说明天交作业", "20002", "同学乙"),
            "skipped_whitelist",
            sender_wl("10001:李老师", "strict"),
            sender_wl("", "off"),
        ),
        (
            "other_group",
            _event("logchk-4", OTHER_GROUP, "别的群的通知", "10001", "李老师"),
            "group_filtered",
            group_wl(f"{GROUP}:测试群"),
            group_wl(""),
        ),
        (
            "dup_first",
            _event("logchk-5", GROUP, "本周五19:00开会", "10001", "李老师"),
            "extracted",
            None,
            None,
        ),
        (
            "dup_second",
            _event("logchk-5", GROUP, "本周五19:00开会", "10001", "李老师"),
            "duplicate",
            None,
            None,
        ),
    ]

    failures = 0
    want_outcomes: list[str] = []

    for name, event, want, before, after in cases:
        if before:
            before()
        cursor = len(handler.lines)
        await handle_message(event)
        produced = handler.lines[cursor:]
        if after:
            after()

        want_outcomes.append(want)
        if len(produced) != 1:
            failures += 1
            print(f"FAIL  {name}: 期望 1 行日志，实际 {len(produced)} 行")
            for line in produced:
                print(f"        {line}")

    print("捕获到的日志：")
    for line in handler.lines:
        print("  " + line)
    print()

    # ---- 断言 1：行数 == 消息数（既不能多也不能少）----
    if len(handler.lines) != len(cases):
        print(f"FAIL  共 {len(cases)} 条消息，却产生了 {len(handler.lines)} 行日志")
        failures += 1
    else:
        print(f"ok    {len(cases)} 条消息 -> 恰好 {len(handler.lines)} 行日志")

    # ---- 断言 2：每条消息都有 msg_id，且都非空 ----
    ids = [m.group(1) for line in handler.lines if (m := MSG_ID.search(line))]
    if len(ids) != len(handler.lines):
        print(f"FAIL  有日志行缺少 msg_id（{len(handler.lines) - len(ids)} 行）")
        failures += 1
    else:
        print(f"ok    每一行都带 msg_id：{sorted(set(ids))}")

    # ---- 断言 3：结果序列与预期一致 ----
    got_outcomes = [m.group(1) for line in handler.lines if (m := OUTCOME.search(line))]
    if got_outcomes != want_outcomes:
        print(f"FAIL  结果序列不符\n      期望 {want_outcomes}\n      实际 {got_outcomes}")
        failures += 1
    else:
        print(f"ok    结果序列与预期一致：{want_outcomes}")

    # ---- 断言 4：抽取成功的行必须能一眼看出抽到了什么 ----
    extracted_lines = [line for line in handler.lines if "结果=extracted" in line]
    for line in extracted_lines:
        for field in ("标题=", "置信度=", "原文="):
            if field not in line:
                print(f"FAIL  extracted 行缺少 {field}：{line}")
                failures += 1
    # 没解析出时间的那条不该硬凑一个截止时间
    if extracted_lines and not failures:
        print(f"ok    extracted 行包含标题/置信度/原文（共 {len(extracted_lines)} 行）")

    # ---- 断言 5：一行就是一行 ----
    if any("\n" in line for line in handler.lines):
        print("FAIL  日志行里出现了换行符，会破坏 grep")
        failures += 1
    else:
        print("ok    所有日志行都是单行")

    await _cleanup()
    msg_logger.removeHandler(handler)
    (
        settings.extractor,
        settings.group_whitelist,
        settings.sender_whitelist,
        settings.sender_whitelist_mode,
    ) = saved
    print()
    if failures:
        print(f"❌ {failures} 项失败")
        return 1
    print("✅ 每条消息恰好一行日志")
    return 0


async def _cleanup() -> None:
    await execute(
        "DELETE FROM notification WHERE raw_message_id IN "
        "(SELECT id FROM raw_message WHERE message_id LIKE 'logchk-%')"
    )
    await execute("DELETE FROM raw_message WHERE message_id LIKE 'logchk-%'")


async def main() -> int:
    await init_db()
    try:
        return await _run()
    finally:
        await close_http()
        await close_db()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
