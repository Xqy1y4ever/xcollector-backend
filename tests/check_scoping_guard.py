"""隔离护栏：碰了用户表的 SQL 必须带 user_id。

    python -m tests.check_scoping_guard

**不需要数据库、不需要起服务** —— 纯粹检查 db.assert_scoped 这个函数本身。

为什么单独给它写测试：它是多用户改造里唯一的"长期防线"。串数据不会崩溃、
不会报错，只会安静地让一个人看到另一个人的通知；靠人 review 守住 30 多个查询
是不现实的。所以护栏自己错了（放过漏过滤的查询、或者误伤正常的查询）
后果都很严重，得单独验。

这里只测**判定逻辑**；"所有真实查询都合规"由 tests/check_isolation.py
在真跑一遍 API 时验证。
"""

from __future__ import annotations

import sys

from app.db import USER_SCOPED_TABLES, UnscopedQueryError, assert_scoped

failures: list[str] = []
total = 0


def check(name: str, got, want) -> None:
    global total
    total += 1
    if got == want:
        print(f"ok    {name}")
    else:
        failures.append(name)
        print(f"FAIL  {name}\n      期望 {want!r}\n      实际 {got!r}")


def check_true(name: str, cond: bool, detail: str = "") -> None:
    check(name + (f"  {detail}" if detail else ""), bool(cond), True)


def allows(sql: str) -> bool:
    try:
        assert_scoped(sql)
        return True
    except UnscopedQueryError:
        return False


print("=== 1. 漏过滤的查询必须被拦下 ===")
for sql in [
    "SELECT * FROM notification WHERE id=?",
    "SELECT * FROM notification",
    "UPDATE notification SET title=? WHERE id=?",
    "DELETE FROM notification WHERE id=?",
    "INSERT INTO correction (id, notification_id, field, value, ts) VALUES (?,?,?,?,?)",
    "SELECT * FROM read_state WHERE notification_id=?",
    "SELECT * FROM gap_alert ORDER BY created_at DESC",
    "UPDATE gap_alert SET acknowledged=1 WHERE id=?",
    "SELECT * FROM pipeline_stat WHERE day=?",
    "INSERT INTO digest_log (id, day, kind, text, sent, error, ts) VALUES (?,?,?,?,?,?,?)",
    'SELECT * FROM bot_state WHERE namespace=? AND "key"=?',
    "SELECT COUNT(*) FROM notification n JOIN correction c ON c.notification_id = n.id",
]:
    check_true(f"拦下：{sql[:52]}…", not allows(sql), sql[:80])

print("\n=== 2. 带了 user_id 的查询必须放过（不能误伤）===")
for sql in [
    "SELECT * FROM notification WHERE user_id=? AND id=?",
    "UPDATE notification SET title=? WHERE id=? AND user_id=?",
    "DELETE FROM notification WHERE id=? AND user_id=?",
    "INSERT INTO correction (id, user_id, notification_id, field, value, actor, ts)"
    " VALUES (?,?,?,?,?,?,?)",
    "SELECT * FROM read_state WHERE notification_id=? AND user_id=?",
    "SELECT * FROM pipeline_stat WHERE user_id=? AND day=?",
    'SELECT * FROM bot_state WHERE user_id=? AND namespace=? AND "key"=?',
    # 子查询里的 user_id 也算数：materialize 的投影就是这种形状
    "SELECT * FROM proj WHERE user_id=? ORDER BY due_at",
]:
    check_true(f"放过：{sql[:52]}…", allows(sql), sql[:80])

print("\n=== 3. 不碰用户表的查询不受影响 ===")
for sql in [
    "SELECT * FROM raw_message WHERE id=?",
    "SELECT * FROM attachment WHERE id=?",
    "SELECT * FROM group_state WHERE group_id=?",
    "SELECT * FROM app_user WHERE qq=?",
    "SELECT * FROM invite_code WHERE code=?",
    "SELECT 1 AS ok",
]:
    check_true(f"无关表照样跑：{sql[:40]}…", allows(sql), sql[:60])

print("\n=== 4. 表名识别不能被列名带偏 ===")
# notification_id / correction_count 这类**列名**里含表名，不能当成碰了表
check_true(
    "带 notification_id 但不碰 notification 表 → 放过",
    allows('SELECT notification_id, user_id, ts FROM correction WHERE user_id=?'),
)
check_true(
    "真的碰了表（FROM correction）且没 user_id → 拦下",
    not allows("SELECT notification_id FROM correction WHERE notification_id=?"),
)

print("\n=== 5. 用户表清单本身 ===")
# 写死清单是刻意的：新增一张用户表就必须来改这里，顺便被迫想一次
# "这张表真的按用户隔离吗"。
check(
    "用户表清单就是契约里那 8 张",
    sorted(USER_SCOPED_TABLES),
    sorted(
        [
            "notification",
            "correction",
            "read_state",
            "gap_alert",
            "pipeline_stat",
            "digest_log",
            "bot_state",
            "subscription",
        ]
    ),
)
for shared in ("raw_message", "attachment", "group_state", "app_user", "invite_code"):
    check_true(f"{shared} 是共享表，不在清单里", shared not in USER_SCOPED_TABLES)

# 订阅表必须在清单里：它是"用户能看到什么"的源头，漏了它等于所有人共用一个订阅表
check_true("subscription 在清单里", "subscription" in USER_SCOPED_TABLES)
check_true(
    "不带 user_id 查订阅 → 拦下",
    not allows("SELECT group_id FROM subscription WHERE group_id=?"),
)
check_true(
    "带 user_id 查订阅 → 放过",
    allows("SELECT group_id FROM subscription WHERE user_id=?"),
)

print()
if failures:
    print(f"❌ {len(failures)}/{total} 条失败：")
    for name in failures:
        print(f"   - {name}")
    sys.exit(1)
print(f"✅ {total} 条断言全部通过")
