"""多用户隔离：A 绝不能看到 B 的任何东西。

需要一个**正在运行的后端**（SIGNUP_MODE=invite）：

    $env:API_TOKEN='service-token'; $env:SIGNUP_MODE='invite'
    $env:DB_PATH='data/iso.db'; $env:ATTACHMENT_DIR='data/iso-att'
    $env:SERVER_PORT='8005'; .\\.venv\\Scripts\\python.exe -m app.main

    $env:ISO_BASE='http://127.0.0.1:8005'; $env:ISO_SERVICE_TOKEN='service-token'
    .\\.venv\\Scripts\\python.exe -m tests.check_isolation

这是多用户改造里**最重要的一条测试**。串数据不会崩溃、不会报错，只会安静地
让一个人看到另一个人的通知 —— 靠"写代码时小心"守不住 30 多个查询，
所以 db.py 里有一道运行时护栏（assert_scoped），这个文件负责真的去撞它。

覆盖：通知列表/详情/修正/已读/删除、统计、摘要记录、bot_state、
附件签名 URL 的归属绑定，以及"服务令牌必须显式说动谁的数据"。
"""

from __future__ import annotations

import os
import sys
import time

import httpx

BASE = os.environ.get("ISO_BASE", "http://127.0.0.1:8005").rstrip("/")
API = BASE + "/api"
SERVICE = os.environ.get("ISO_SERVICE_TOKEN", "service-token")

RUN = os.environ.get("ISO_RUN") or str(int(time.time() * 1000))
QQ_A = str(200000000 + int(RUN[-7:]) % 80000000)
QQ_B = str(int(QQ_A) + 1)

H = {"Authorization": f"Bearer {SERVICE}"}
c = httpx.Client(timeout=20)
fails: list[str] = []
total = 0


def check(name: str, got, want) -> None:
    global total
    total += 1
    if got == want:
        print(f"ok    {name}")
    else:
        fails.append(name)
        print(f"FAIL  {name}\n      期望 {want!r}\n      实际 {got!r}")


def check_true(name: str, cond: bool, detail: str = "") -> None:
    check(name + (f"  {detail}" if detail else ""), bool(cond), True)


def hdr(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def register(qq: str) -> tuple[str, str]:
    """注册一个用户，返回 (user_id, token)。"""
    code = c.post(f"{API}/verify/request", json={"qq": qq}, headers=H).json()["code"]
    invite = c.post(
        f"{API}/invites", json={"note": f"iso-{RUN}", "max_uses": 1}, headers=H
    ).json()["code"]
    r = c.post(f"{API}/register", json={"qq": qq, "code": code, "invite_code": invite})
    assert r.status_code == 200, r.text
    body = r.json()
    return body["user"]["id"], body["token"]


print(f"→ {API}  RUN={RUN}\n")

# ---------------------------------------------------------------------------
print("--- 准备：注册两个用户 ---")
uid_a, tok_a = register(QQ_A)
uid_b, tok_b = register(QQ_B)
check_true("A 拿到 user_id", uid_a.startswith("usr"), uid_a)
check_true("B 拿到另一个 user_id", uid_b != uid_a, f"{uid_a} vs {uid_b}")
print(f"    A={uid_a}  B={uid_b}")

# ---------------------------------------------------------------------------
print("\n--- 服务令牌必须显式说动谁的数据 ---")
r = c.get(f"{API}/notifications", headers=H)
check("服务令牌不带 user_id 读通知 → 400", r.status_code, 400)
check_true("报错说清了原因", "user_id" in r.text, r.text[:140])

ts = int(time.time() * 1000)

# 一条原始消息，两个人各订阅 → 扇出成两条通知
r = c.post(
    f"{API}/messages",
    headers=H,
    json={
        "message_id": f"m-{RUN}",
        "group_id": f"g-{RUN}",
        "group_name": "隔离测试群",
        "sender_id": "10001",
        "sender_name": "张老师",
        "ts": ts,
        "content": "本体",
    },
)
raw_id = r.json()["id"]
check("原始消息入库（共享层）→ 200", r.status_code, 200)

notif_ids: dict[str, str] = {}
for label, uid in (("A", uid_a), ("B", uid_b)):
    r = c.post(
        f"{API}/notifications?user_id={uid}",
        headers=H,
        json={
            "raw_message_id": raw_id,
            "group_id": f"g-{RUN}",
            "sender_id": "10001",
            "source_ts": ts,
            "title": f"{label} 的任务-{RUN}",
            "evidence": "证据",
        },
    )
    check(f"给 {label} 建条 → 200", r.status_code, 200)
    notif_ids[label] = r.json()["id"]

check_true(
    "同一条原文扇出成两个不同的条目 id",
    notif_ids["A"] != notif_ids["B"],
    str(notif_ids),
)

# ---------------------------------------------------------------------------
print("\n--- 列表：各自只看得到自己的 ---")
r = c.get(f"{API}/notifications", headers=hdr(tok_a))
check("A 读列表 → 200", r.status_code, 200)
list_a = r.json()["notifications"]
check("A 只看到 1 条", len(list_a), 1)
check_true("A 看到的是自己的标题", list_a[0]["title"].startswith("A "), list_a[0]["title"])

r = c.get(f"{API}/notifications", headers=hdr(tok_b))
list_b = r.json()["notifications"]
check("B 只看到 1 条", len(list_b), 1)
check_true("B 看到的是自己的标题", list_b[0]["title"].startswith("B "), list_b[0]["title"])

r = c.get(f"{API}/notifications?count_only=1", headers=hdr(tok_a))
check("A 的计数是 1", r.json()["count"], 1)

# ---------------------------------------------------------------------------
print("\n--- 详情 / 修改 / 删除：碰别人的一律 404 ---")
r = c.get(f"{API}/notifications/{notif_ids['B']}", headers=hdr(tok_a))
check("A 读 B 的条目详情 → 404", r.status_code, 404)

r = c.get(f"{API}/notifications/{notif_ids['A']}", headers=hdr(tok_a))
check("A 读自己的条目 → 200", r.status_code, 200)
check("详情里的标题是自己的", r.json()["notification"]["title"].startswith("A "), True)

r = c.patch(
    f"{API}/notifications/{notif_ids['B']}", headers=hdr(tok_a), json={"title": "被改了"}
)
check("用户令牌改条目 → 403（PATCH 只给 bot 用）", r.status_code, 403)

# 归属检查要用**真正会调它的身份**去撞：PATCH/DELETE 是服务令牌专属，
# 所以这里带服务令牌 + A 的 user_id 去动 B 的条目。
r = c.patch(
    f"{API}/notifications/{notif_ids['B']}?user_id={uid_a}",
    headers=H,
    json={"title": "被改了"},
)
check("服务令牌拿 A 的身份改 B 的条目 → 404", r.status_code, 404)

r = c.patch(
    f"{API}/notifications/{notif_ids['A']}?user_id={uid_a}", headers=H, json={"title": "核对"}
)
check("服务令牌拿 A 的身份改 A 的条目 → 200", r.status_code, 200)

r = c.post(
    f"{API}/notifications/{notif_ids['B']}/corrections",
    headers=hdr(tok_a),
    json={"field": "title", "value": "被改了"},
)
check("A 对 B 的条目提交修正 → 404", r.status_code, 404)

r = c.post(f"{API}/notifications/{notif_ids['B']}/read", headers=hdr(tok_a), json={"read": True})
check("A 把 B 的条目标已读 → 404", r.status_code, 404)

r = c.delete(f"{API}/notifications/{notif_ids['B']}?user_id={uid_a}", headers=H)
check("服务令牌拿 A 的身份删 B 的条目 → 404", r.status_code, 404)

# ---------------------------------------------------------------------------
print("\n--- 修正与已读：状态互不影响 ---")
r = c.post(
    f"{API}/notifications/{notif_ids['A']}/corrections",
    headers=hdr(tok_a),
    json={"field": "title", "value": "A 改过的标题"},
)
check("A 改自己的条目 → 200", r.status_code, 200)

r = c.get(f"{API}/notifications/{notif_ids['A']}", headers=hdr(tok_a))
check("A 看到自己的修正", r.json()["notification"]["title"], "A 改过的标题")

r = c.get(f"{API}/notifications/{notif_ids['B']}", headers=hdr(tok_b))
check_true(
    "B 的标题**不受** A 的修正影响",
    r.json()["notification"]["title"].startswith("B "),
    r.json()["notification"]["title"],
)
check("B 的 manually_edited 仍是 false", r.json()["notification"]["manually_edited"], False)

c.post(f"{API}/notifications/{notif_ids['A']}/read", headers=hdr(tok_a), json={"read": True})
r = c.get(f"{API}/notifications/{notif_ids['A']}", headers=hdr(tok_a))
check("A 标了已读", r.json()["notification"]["read"], True)
r = c.get(f"{API}/notifications/{notif_ids['B']}", headers=hdr(tok_b))
check("B 没被连带标已读", r.json()["notification"]["read"], False)

# ---------------------------------------------------------------------------
print("\n--- 统计 / 摘要记录 / bot_state 也是分开的 ---")
c.post(f"{API}/stats?user_id={uid_a}", headers=H, json={"fields": {"ingested": 7}})
r = c.get(f"{API}/stats", headers=hdr(tok_a))
check("A 的统计是 7", r.json()["ingested"], 7)
r = c.get(f"{API}/stats", headers=hdr(tok_b))
check("B 的统计是 0（不是 7）", r.json()["ingested"], 0)

c.post(
    f"{API}/digest-log?user_id={uid_a}",
    headers=H,
    json={"kind": "auto", "day": "2026-09-16", "text": "A 的摘要", "sent": True},
)
r = c.get(f"{API}/digest-log?day=2026-09-16&count_only=1", headers=hdr(tok_a))
check("A 有 1 条摘要记录", r.json()["count"], 1)
r = c.get(f"{API}/digest-log?day=2026-09-16&count_only=1", headers=hdr(tok_b))
check("B 有 0 条（拿不到 A 的）", r.json()["count"], 0)

# 两个用户用同一个 key 写 state —— 这是最容易互相覆盖的地方
for uid in (uid_a, uid_b):
    c.put(
        f"{API}/state/command_pending/k?user_id={uid}",
        headers=H,
        json={"value": {"owner": uid}},
    )
r = c.get(f"{API}/state/command_pending/k", headers=hdr(tok_a))
check("A 读回自己的 state", r.json()["value"]["owner"], uid_a)
r = c.get(f"{API}/state/command_pending/k", headers=hdr(tok_b))
check("B 读回自己的（没被 A 覆盖）", r.json()["value"]["owner"], uid_b)

r = c.get(f"{API}/state/command_pending", headers=hdr(tok_a), params={"count_only": "1"})
check("A 的 namespace 里只有 1 个键", r.json()["count"], 1)

# ---------------------------------------------------------------------------
print("\n--- 附件签名 URL 绑定用户 ---")
PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
    "890000000a49444154789c63000100000500010d0a2db40000000049454e44ae426082"
)
r = c.post(
    f"{API}/attachments?user_id={uid_a}",
    headers=H,
    files={"file": ("a.png", PNG, "image/png")},
    data={"filename": "a.png"},
)
att_id = r.json()["id"]
c.patch(
    f"{API}/messages/{raw_id}?user_id={uid_a}",
    headers=H,
    json={"attachments": [{"id": att_id, "url": r.json()["url"], "type": "image"}]},
)

r = c.get(f"{API}/notifications/{notif_ids['A']}", headers=hdr(tok_a))
signed_a = r.json()["notification"]["attachments"][0]["url"]
check_true("A 的附件链接带 u=", "u=" in signed_a, signed_a)

r = c.get(BASE + signed_a)
check("A 的链接不带任何头能取到 → 200", r.status_code, 200)

# 把 u 改成 B：签名覆盖了 user_id，所以必须验不过
tampered = signed_a.replace(f"u={uid_a}", f"u={uid_b}")
check_true("篡改后的链接确实和原来不同", tampered != signed_a)
r = c.get(BASE + tampered)
check("把 u 换成 B → 401（签名覆盖了 user_id）", r.status_code, 401)

print()
if fails:
    print(f"❌ {len(fails)}/{total} 条失败：")
    for name in fails:
        print(f"   - {name}")
    sys.exit(1)
print(f"✅ {total} 条断言全部通过")
