"""验证「人工修正不会被重跑抽取覆盖」。

    python -m tests.check_corrections

这是 correction 表独立于 notification 表存在的全部理由：
改 prompt、换模型、重跑全量历史时，人工改过的 DDL 必须原样保留。
"""

from __future__ import annotations

import sys

from app.db import add_correction, close_db, execute, fetch_one, init_db, upsert_notification
from app.materialize import build_view

SQL_ONE = """SELECT n.*, r.attachments FROM notification n
             LEFT JOIN raw_message r ON r.id = n.raw_message_id WHERE n.id = ?"""


async def main() -> int:
    await init_db()
    try:
        return await _run()
    finally:
        # 必须保证关闭：aiosqlite 的工作线程不是 daemon，
        # 异常退出时若没关连接，解释器会一直挂着不退出。
        await close_db()


async def _run() -> int:
    row = await fetch_one(
        "SELECT * FROM notification WHERE due_at IS NOT NULL ORDER BY due_at LIMIT 1"
    )
    if row is None:
        print("❌ 库里没有带 due_at 的通知，请先跑 python -m app.tools.seed_demo")
        return 1

    nid = row["id"]
    machine_value = int(row["due_at"])
    human_value = machine_value + 7 * 24 * 3600 * 1000  # 人工改成往后一周

    print(f"通知：{row['title']}")
    print(f"机器值：{machine_value}")

    # ---- 1. 人工修正 ----
    await add_correction(nid, "due_at", human_value, user_id="selftest")
    await add_correction(nid, "title", "（人工改过的标题）", user_id="selftest")

    view = await build_view(await fetch_one(SQL_ONE, (nid,)))
    checks = [
        ("人工修正立即生效", view["due_at"] == human_value),
        ("manually_edited 标记为真", view["manually_edited"] is True),
        ("标题修正生效", view["title"] == "（人工改过的标题）"),
    ]

    # ---- 2. 模拟重跑抽取（覆盖 notification 表，correction 表不动）----
    await upsert_notification(
        {
            "raw_message_id": row["raw_message_id"],
            "group_id": row["group_id"],
            "group_name": row["group_name"],
            "sender_id": row["sender_id"],
            "sender_name": row["sender_name"],
            "source_ts": row["source_ts"],
            "title": "重跑后的机器标题",
            "summary": row["summary"],
            "due_at": machine_value,
            "due_text": row["due_text"],
            "due_confidence": row["due_confidence"],
            "evidence": row["evidence"],
            "conflict": False,
            "candidates": [],
            "extractor": "rerun",
            "model": "rerun",
            "prompt_ver": "rerun",
        }
    )

    after = await build_view(await fetch_one(SQL_ONE, (nid,)))
    checks += [
        ("重跑后人工时间仍在", after["due_at"] == human_value),
        ("重跑后人工标题仍在", after["title"] == "（人工改过的标题）"),
        ("重跑确实更新了机器字段", (after["prompt_ver"] or "") == "rerun"),
    ]

    # ---- 3. 清理 ----
    await execute("DELETE FROM correction WHERE notification_id=? AND user_id='selftest'", (nid,))
    await upsert_notification(
        {
            "raw_message_id": row["raw_message_id"],
            "group_id": row["group_id"],
            "group_name": row["group_name"],
            "sender_id": row["sender_id"],
            "sender_name": row["sender_name"],
            "source_ts": row["source_ts"],
            "title": row["title"],
            "summary": row["summary"],
            "due_at": machine_value,
            "due_text": row["due_text"],
            "due_confidence": row["due_confidence"],
            "evidence": row["evidence"],
            "conflict": bool(row["conflict"]),
            "candidates": [],
            "extractor": row["extractor"],
            "model": row["model"],
            "prompt_ver": row["prompt_ver"],
        }
    )

    print()
    failures = 0
    for name, ok in checks:
        print(("ok    " if ok else "FAIL  ") + name)
        failures += 0 if ok else 1

    await close_db()
    print()
    if failures:
        print(f"❌ {failures} 项失败")
        return 1
    print("✅ 人工修正不被重跑覆盖")
    return 0


if __name__ == "__main__":
    import asyncio

    sys.exit(asyncio.run(main()))
