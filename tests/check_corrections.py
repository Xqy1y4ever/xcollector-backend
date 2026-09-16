"""验证「人工修正不会被重跑覆盖」。

    python -m tests.check_corrections

这是 correction 表独立于 notification 表存在的全部理由：
改 prompt、换模型、重跑全量历史时，人工改过的 DDL 必须原样保留。

脚本自己在库里造一条自检数据（message_id 以 `selftest-` 开头），
不依赖任何演示数据；跑完会把它连同修正一起删掉，不留在库里。
"""

from __future__ import annotations

import sys

from app.db import (
    add_correction,
    close_db,
    execute,
    fetch_one,
    get_raw,
    init_db,
    insert_raw_message,
    upsert_notification,
)
from app.materialize import get_notification_view
from app.utils import now_ms

SELFTEST_MESSAGE_ID = "selftest-corrections"
GROUP_ID = "selftest-group"


def machine_payload(raw_id: str, *, title: str, due_at: int, prompt_ver: str) -> dict:
    return {
        "raw_message_id": raw_id,
        "group_id": GROUP_ID,
        "group_name": "自检群",
        "sender_id": "selftest",
        "sender_name": "自检",
        "source_ts": due_at - 3600_000,
        "title": title,
        "summary": "自检摘要",
        "location": "教三201",
        "due_at": due_at,
        "due_text": "下周三前",
        "due_confidence": 0.72,
        "evidence": "自检用原文依据",
        "conflict": False,
        "candidates": [],
        "prompt_ver": prompt_ver,
    }


async def main() -> int:
    await init_db()
    try:
        return await _run()
    finally:
        # 必须保证关闭：aiosqlite 的工作线程不是 daemon，
        # 异常退出时若没关连接，解释器会一直挂着不退出。
        await close_db()


async def _run() -> int:
    # ---- 0. 造一条干净的自检数据（重复运行也是同一个 id）----
    machine_due = now_ms() + 3 * 24 * 3600 * 1000
    raw_id, _ = await insert_raw_message(
        message_id=SELFTEST_MESSAGE_ID,
        group_id=GROUP_ID,
        group_name="自检群",
        sender_id="selftest",
        sender_name="自检",
        ts=now_ms(),
        content="自检：人工修正保护",
        attachments=[],
        raw={"selftest": True},
    )
    # 先把上一次可能残留的修正与机器字段清干净，让结果可重复
    await execute("DELETE FROM correction WHERE notification_id IN (SELECT id FROM notification WHERE raw_message_id=?)", (raw_id,))
    original = machine_payload(raw_id, title="机器标题 v1", due_at=machine_due, prompt_ver="v1")
    notif_id, created = await upsert_notification(original)
    print(f"自检通知：{notif_id}（{'新建' if created else '复用'}）")
    print(f"机器值 due_at：{machine_due}")

    before = await get_notification_view(notif_id)
    checks = [
        ("初始没有人工修正", before["manually_edited"] is False),
        ("初始用机器值", before["due_at"] == machine_due and before["title"] == "机器标题 v1"),
        ("raw_message 行存在", (await get_raw(raw_id)) is not None),
    ]

    human_due = machine_due + 7 * 24 * 3600 * 1000  # 人工改成往后一周

    # ---- 1. 人工修正立即生效 ----
    await add_correction(notif_id, "due_at", human_due, user_id="selftest")
    await add_correction(notif_id, "title", "（人工改过的标题）", user_id="selftest")

    view = await get_notification_view(notif_id)
    checks += [
        ("人工修正立即生效", view["due_at"] == human_due),
        ("manually_edited 标记为真", view["manually_edited"] is True),
        ("标题修正生效", view["title"] == "（人工改过的标题）"),
    ]

    # ---- 2. 模拟重跑（覆盖 notification 表的机器字段，correction 表不动）----
    await upsert_notification(
        machine_payload(raw_id, title="重跑后的机器标题", due_at=machine_due, prompt_ver="rerun")
    )

    after = await get_notification_view(notif_id)
    checks += [
        ("重跑后人工时间仍在", after["due_at"] == human_due),
        ("重跑后人工标题仍在", after["title"] == "（人工改过的标题）"),
        ("重跑确实更新了机器字段", (after["prompt_ver"] or "") == "rerun"),
        ("重跑后机器字段在库里的值也变了", (await fetch_one("SELECT prompt_ver FROM notification WHERE id=?", (notif_id,)))["prompt_ver"] == "rerun"),
    ]

    # ---- 3. status 同理：人工 status 优先于 due_at 推导 ----
    await add_correction(notif_id, "status", "done", user_id="selftest")
    view = await get_notification_view(notif_id)
    checks += [
        ("人工 status 覆盖推导", view["status"] == "done"),
    ]

    # ---- 4. 清理：删掉自检修正、复原机器字段 ----
    await execute("DELETE FROM correction WHERE notification_id=? AND user_id='selftest'", (notif_id,))
    await upsert_notification(
        machine_payload(raw_id, title="机器标题 v1", due_at=machine_due, prompt_ver="v1")
    )
    restored = await get_notification_view(notif_id)
    checks += [
        ("清理后不再标记人工修正", restored["manually_edited"] is False),
        ("清理后回到机器值", restored["due_at"] == machine_due and restored["title"] == "机器标题 v1"),
        ("清理后 status 回到推导值", restored["status"] in ("active", "expired")),
    ]

    # ---- 5. 收尾：自检数据用完即删，不留在库里 ----
    # （删的是本脚本自己造的那一条，不是真的入库消息。）
    await execute("DELETE FROM correction WHERE notification_id=?", (notif_id,))
    await execute("DELETE FROM read_state WHERE notification_id=?", (notif_id,))
    await execute("DELETE FROM notification WHERE id=?", (notif_id,))
    await execute("DELETE FROM raw_message WHERE id=?", (raw_id,))
    checks += [
        ("自检数据已清理", await get_notification_view(notif_id) is None),
    ]

    print()
    failures = 0
    for name, ok in checks:
        print(("ok    " if ok else "FAIL  ") + name)
        failures += 0 if ok else 1

    print()
    if failures:
        print(f"❌ {failures} 项失败")
        return 1
    print("✅ 人工修正不被重跑覆盖")
    return 0


if __name__ == "__main__":
    import asyncio

    sys.exit(asyncio.run(main()))
