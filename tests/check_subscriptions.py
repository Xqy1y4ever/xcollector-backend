"""订阅：最小单位是 (群, 发送者)，**没有"整个群"这个选项**。

需要一个**正在运行的后端**：

    $env:API_TOKEN='service-token'; $env:SERVER_PORT='8005'
    .\\.venv\\Scripts\\python.exe -m app.main

    $env:SUB_BASE='http://127.0.0.1:8005'; $env:SUB_SERVICE_TOKEN='service-token'
    .\\.venv\\Scripts\\python.exe -m tests.check_subscriptions

这个文件守三件事：

  1. **禁止整个群**。空发送者、`*`、`all`、`全部`、逗号分隔的多个 —— 全部 400，
     而且报错要说人话（用户得知道该填什么）。这是产品上最容易被"顺手放宽"的
     一条规则，所以它有很多条断言。
  2. **订阅是用户私有的**。A 看不到、改不了、删不了 B 的订阅；id 也探测不出来。
  3. **路由**。`GET /subscriptions/routing` 是 bot 的投递名单 —— 它是"并集处理、
     按订阅扇出"里"扇给谁"这一步。订错了/漏了会直接变成"某人收不到通知"，
     所以正反两面都要测：订了才在名单里，关掉就不在，别人的群/发送者不在。
"""

from __future__ import annotations

import os
import sys
import time

import httpx

BASE = os.environ.get("SUB_BASE", "http://127.0.0.1:8005").rstrip("/")
API = BASE + "/api"
SERVICE = os.environ.get("SUB_SERVICE_TOKEN", "service-token")

RUN = os.environ.get("SUB_RUN") or str(int(time.time() * 1000))
QQ_A = str(300000000 + int(RUN[-7:]) % 60000000)
QQ_B = str(int(QQ_A) + 1)
QQ_C = str(int(QQ_A) + 2)

# 群号/发送者**必须每轮都不一样**。用常量的话，上一轮留下的订阅还在库里
# （尤其是第 9 节那 200 条上限测试），路由断言里的精确列表比对就会撞上
# 历史用户 —— 一个测试跑第二遍就红，比没有测试更糟。
# 形状仍然全是合法 QQ 号，保证被测的是"订阅"而不是"参数格式"。
_SUF = RUN[-7:]
GROUP_1 = "71" + _SUF
GROUP_2 = "72" + _SUF
SENDER_1 = "81" + _SUF
SENDER_2 = "82" + _SUF
# 这两个从没被订过，用来验证"没订就没有名字在名单里"
GROUP_NONE = "73" + _SUF
SENDER_NONE = "83" + _SUF

H = {"Authorization": f"Bearer {SERVICE}"}
c = httpx.Client(timeout=30)
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
    code = c.post(f"{API}/verify/request", json={"qq": qq}, headers=H).json()["code"]
    invite = c.post(
        f"{API}/invites", json={"note": f"sub-{RUN}", "max_uses": 1}, headers=H
    ).json()["code"]
    r = c.post(f"{API}/register", json={"qq": qq, "code": code, "invite_code": invite})
    assert r.status_code == 200, r.text
    body = r.json()
    return body["user"]["id"], body["token"]


def add(tok: str, group: str, sender: str, **extra):
    return c.post(
        f"{API}/subscriptions",
        json={"group_id": group, "sender_id": sender, **extra},
        headers=hdr(tok),
    )


def routing(group: str, sender: str):
    r = c.get(
        f"{API}/subscriptions/routing",
        params={"group_id": group, "sender_id": sender},
        headers=H,
    )
    assert r.status_code == 200, r.text
    return r.json()["user_ids"]


print(f"→ {API}  RUN={RUN}\n")

# ---------------------------------------------------------------------------
print("--- 准备：注册三个用户（第三个专门用来撞上限）---")
uid_a, tok_a = register(QQ_A)
uid_b, tok_b = register(QQ_B)
print(f"    A={uid_a}  B={uid_b}")

# ---------------------------------------------------------------------------
print("\n--- 1. 鉴权 ---")
r = c.get(f"{API}/subscriptions")
check("没令牌 → 401", r.status_code, 401)

r = c.get(f"{API}/subscriptions", headers=H)
check("服务令牌不带 user_id → 400（不知道要动谁）", r.status_code, 400)

r = c.get(f"{API}/subscriptions", params={"user_id": uid_a}, headers=H)
check("服务令牌带 user_id → 200", r.status_code, 200)
check("新用户还没有订阅", r.json()["subscriptions"], [])

r = c.get(f"{API}/subscriptions", headers=hdr(tok_a))
check("用户令牌看自己的订阅 → 200", r.status_code, 200)

# ---------------------------------------------------------------------------
print("\n--- 2. 禁止「整个群」：这是本文件的核心 ---")
cases = [
    ("空发送者", "", "整个群"),
    ("星号", "*", "整个群"),
    ("all", "all", "整个群"),
    ("any", "any", "整个群"),
    ("全部", "全部", "整个群"),
    ("全群", "全群", "整个群"),
    ("整个群", "整个群", "整个群"),
]
for label, sender, must_say in cases:
    r = add(tok_a, GROUP_1, sender)
    check(f"发送者={label} → 400", r.status_code, 400)
    check_true(f"  报错说清了不支持整个群（{label}）", must_say in r.text, r.text[:160])

r = add(tok_a, GROUP_1, f"{SENDER_1},{SENDER_2}")
check("逗号分隔两个发送者 → 400（不给批量订阅留口子）", r.status_code, 400)

for bad in ("abc", "0123456", "1"):
    r = add(tok_a, GROUP_1, bad)
    check(f"发送者={bad!r} 不像 QQ 号 → 400", r.status_code, 400)

for bad in ("", "abc", "*"):
    r = add(tok_a, bad, SENDER_1)
    check(f"群号={bad!r} 不像群号 → 400", r.status_code, 400)

# 反面：一条都不该被写进去
r = c.get(f"{API}/subscriptions", headers=hdr(tok_a))
check("上面全部失败后，一条订阅都没落库", r.json()["count"], 0)

# ---------------------------------------------------------------------------
print("\n--- 3. 正常订阅 ---")
r = add(tok_a, GROUP_1, SENDER_1, group_name="官方通知群", sender_name="教务处", note="只关注 ddl")
check("订 (群1, 发送者1) → 200", r.status_code, 200)
body = r.json()
check("是新创建的", body["created"], True)
sub_a1 = body["subscription"]["id"]
check_true("返回体里没有 user_id（调用方已经知道是谁）", "user_id" not in body["subscription"], str(body["subscription"]))
check("group_id 原样", body["subscription"]["group_id"], GROUP_1)
check("sender_id 原样", body["subscription"]["sender_id"], SENDER_1)
check("默认是启用的", body["subscription"]["enabled"], True)

r = add(tok_a, GROUP_1, SENDER_1)
check("重复订阅同一个来源 → 200（幂等，不是 409）", r.status_code, 200)
check("重复订阅 → created=false", r.json()["created"], False)
check("重复订阅不会多出一行", r.json()["subscription"]["id"], sub_a1)

r = add(tok_a, GROUP_1, SENDER_2)
sub_a2 = r.json()["subscription"]["id"]
r = add(tok_a, GROUP_2, SENDER_1)
sub_a3 = r.json()["subscription"]["id"]
r = c.get(f"{API}/subscriptions", headers=hdr(tok_a))
check("A 现在有 3 条订阅", r.json()["count"], 3)

# ---------------------------------------------------------------------------
print("\n--- 4. 路由：bot 的投递名单 ---")
check("(群1, 发送者1) 的名单里有 A", routing(GROUP_1, SENDER_1), [uid_a])
check("(群1, 发送者2) 的名单里也有 A", routing(GROUP_1, SENDER_2), [uid_a])
check("(群2, 发送者1) 的名单里也有 A", routing(GROUP_2, SENDER_1), [uid_a])
check("没订过的发送者 → 空名单", routing(GROUP_1, SENDER_NONE), [])
check("没订过的群 → 空名单", routing(GROUP_NONE, SENDER_1), [])

r = add(tok_b, GROUP_1, SENDER_1)
check("B 也订同一个来源 → 200", r.status_code, 200)
check("名单变成两个人（并集）", sorted(routing(GROUP_1, SENDER_1)), sorted([uid_a, uid_b]))
check("B 的那条不影响别的来源", routing(GROUP_2, SENDER_1), [uid_a])

r = c.get(f"{API}/subscriptions/routing", params={"group_id": GROUP_1, "sender_id": SENDER_1}, headers=hdr(tok_a))
check("用户令牌不能读全局投递名单 → 403", r.status_code, 403)

# ---------------------------------------------------------------------------
print("\n--- 5. 关掉就不投递 ---")
r = c.patch(f"{API}/subscriptions/{sub_a1}", json={"enabled": False}, headers=hdr(tok_a))
check("关掉订阅 → 200", r.status_code, 200)
check("返回里 enabled=false", r.json()["subscription"]["enabled"], False)
check("关掉后 A 不在 (群1,发送者1) 的名单里", routing(GROUP_1, SENDER_1), [uid_b])
check("关掉一条不影响 A 的其它订阅", routing(GROUP_2, SENDER_1), [uid_a])

r = c.get(f"{API}/subscriptions", headers=hdr(tok_a))
check("默认列出被关掉的（前端要能再打开）", r.json()["count"], 3)
r = c.get(f"{API}/subscriptions", params={"include_disabled": "false"}, headers=hdr(tok_a))
check("include_disabled=false → 只剩 2 条", r.json()["count"], 2)

r = add(tok_a, GROUP_1, SENDER_1)
check("重新订同一个来源 → created=false", r.json()["created"], False)
check("重新订会把它打开", r.json()["subscription"]["enabled"], True)
check("打开后 A 又回到名单里", sorted(routing(GROUP_1, SENDER_1)), sorted([uid_a, uid_b]))
check("之前记下的群名没被抹掉", r.json()["subscription"]["group_name"], "官方通知群")

r = c.patch(f"{API}/subscriptions/{sub_a2}", json={"note": "改个备注"}, headers=hdr(tok_a))
check("改备注 → 200", r.status_code, 200)
check("备注改掉了", r.json()["subscription"]["note"], "改个备注")

r = c.patch(f"{API}/subscriptions/{sub_a2}", json={}, headers=hdr(tok_a))
check("什么都不改 → 400（不是静默成功）", r.status_code, 400)

# ---------------------------------------------------------------------------
print("\n--- 6. 订阅是用户私有的 ---")
r = c.get(f"{API}/subscriptions", headers=hdr(tok_b))
check("B 只看到自己的 1 条", r.json()["count"], 1)
check_true("B 的列表里没有 A 的那条", sub_a1 not in [s["id"] for s in r.json()["subscriptions"]])

r = c.patch(f"{API}/subscriptions/{sub_a2}", json={"enabled": False}, headers=hdr(tok_b))
check("B 关 A 的订阅 → 404（不是 403，id 探测不出来）", r.status_code, 404)

r = c.delete(f"{API}/subscriptions/{sub_a2}", headers=hdr(tok_b))
check("B 删 A 的订阅 → 404", r.status_code, 404)

r = c.get(f"{API}/subscriptions", headers=hdr(tok_a))
check("A 的订阅一条都没少", r.json()["count"], 3)

# 服务令牌带 B 的 user_id 也拿不到 A 的
r = c.get(f"{API}/subscriptions", params={"user_id": uid_b}, headers=H)
check("服务令牌按 B 的身份读 → 只有 1 条", r.json()["count"], 1)

# ---------------------------------------------------------------------------
print("\n--- 7. 删除 ---")
r = c.delete(f"{API}/subscriptions/{sub_a3}", headers=hdr(tok_a))
check("删自己的订阅 → 200", r.status_code, 200)
r = c.get(f"{API}/subscriptions", headers=hdr(tok_a))
check("删完剩 2 条", r.json()["count"], 2)
check("删掉后不在 (群2,发送者1) 的名单里", routing(GROUP_2, SENDER_1), [])

r = c.delete(f"{API}/subscriptions/{sub_a3}", headers=hdr(tok_a))
check("重复删 → 404", r.status_code, 404)

# 删了以后可以重新订（走的是"新建"这条路，id 会变）
r = add(tok_a, GROUP_2, SENDER_1)
check("删掉后能重新订", r.status_code, 200)
check("重新订是新建", r.json()["created"], True)

# ---------------------------------------------------------------------------
print("\n--- 8. 信息源目录 ---")
ts = int(time.time() * 1000)
c.post(
    f"{API}/messages",
    headers=H,
    json={
        "message_id": f"src-{RUN}",
        "group_id": f"g-{RUN}",
        "group_name": f"目录测试群-{RUN}",
        "sender_id": "900000001",
        "sender_name": "目录里的老师",
        "ts": ts,
        "content": "本体",
    },
)

r = c.get(f"{API}/sources", headers=hdr(tok_a))
check("用户令牌也能看目录 → 200", r.status_code, 200)
sources = r.json()["sources"]
mine = [s for s in sources if s["group_id"] == f"g-{RUN}"]
check("目录里有刚见过的来源", len(mine), 1)
check_true("目录项带群名", (mine[0].get("group_name") or "").startswith("目录测试群"), str(mine[:1]))
check_true("目录项带发送者名", mine[0].get("sender_name") == "目录里的老师", str(mine[:1]))
check_true("目录项**不含 user_id**（否则就是按用户的数据泄露）", "user_id" not in mine[0], str(mine[:1]))

r = c.get(f"{API}/sources", params={"keyword": f"目录测试群-{RUN}"}, headers=hdr(tok_a))
check("按群名搜得到", len(r.json()["sources"]) >= 1, True)
r = c.get(f"{API}/sources", params={"keyword": "绝对不存在的群名"}, headers=hdr(tok_a))
check("搜不到就是空", r.json()["sources"], [])

r = c.get(f"{API}/sources")
check("目录也要登录 → 401", r.status_code, 401)

# ---------------------------------------------------------------------------
print("\n--- 9. 订阅数上限 ---")
uid_c, tok_c = register(QQ_C)
cap = 0
last = None
for i in range(0, 260):
    last = add(tok_c, GROUP_1, str(10000000 + i))
    if last.status_code == 400:
        cap = i
        break
check("到某个数量就拒绝新增", cap > 0, True)
check_true("拒绝的理由说清了是上限", "上限" in (last.text if last else ""), (last.text if last else "")[:160])
check_true("上限是 200 条", cap == 200, f"实际第 {cap + 1} 条被拒")

r = c.get(f"{API}/subscriptions", headers=hdr(tok_c))
check("被拒的那条没有落库", r.json()["count"], 200)

# 订满了以后，把已有的一条重新打开不该被上限挡住
r = c.get(f"{API}/subscriptions", headers=hdr(tok_c))
some_id = r.json()["subscriptions"][0]["id"]
c.patch(f"{API}/subscriptions/{some_id}", json={"enabled": False}, headers=hdr(tok_c))
r = c.patch(f"{API}/subscriptions/{some_id}", json={"enabled": True}, headers=hdr(tok_c))
check("订满后仍能操作已有订阅 → 200", r.status_code, 200)

print()
if fails:
    print(f"❌ {len(fails)}/{total} 条失败：")
    for name in fails:
        print(f"   - {name}")
    sys.exit(1)
print(f"✅ {total} 条断言全部通过")
