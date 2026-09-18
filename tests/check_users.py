"""用户系统：注册、邀请码、QQ 验证码、服务/用户两种令牌。

需要一个**正在运行的后端**：

    # 终端 1
    $env:API_TOKEN='service-token'; $env:SIGNUP_MODE='invite'
    $env:DB_PATH='data/users.db'; $env:ATTACHMENT_DIR='data/users-att'
    $env:SERVER_PORT='8004'; .\\.venv\\Scripts\\python.exe -m app.main

    # 终端 2
    $env:USERS_BASE='http://127.0.0.1:8004'; $env:USERS_SERVICE_TOKEN='service-token'
    .\\.venv\\Scripts\\python.exe -m tests.check_users

覆盖（每条都是一次真的 HTTP 请求）：
  - 两种令牌的范围：服务令牌能写、用户令牌写不了（403）、坏令牌 401
  - 注册全流程：要验证码 → 邀请码 → 注册 → 拿到令牌 → 用它登录
  - **一次性语义**：验证码用过即废、邀请码用完即废
  - **防暴力**：验证码猜错到上限就作废，之后连对的码也不认
  - **轮换**：已有用户再走一次流程拿到新令牌，旧令牌立刻失效
  - `resolve_owner`：用户令牌的归属永远是它自己，服务令牌必须显式说动谁的数据

每次运行用随机 QQ 号造数据，重复运行是安全的。
"""

from __future__ import annotations

import os
import sys
import time

import httpx

BASE = os.environ.get("USERS_BASE", "http://127.0.0.1:8004").rstrip("/")
API = BASE + "/api"
SERVICE = os.environ.get("USERS_SERVICE_TOKEN", "service-token")

# 随机 QQ 号：避免重复运行撞 UNIQUE 约束
RUN = os.environ.get("USERS_RUN") or str(int(time.time() * 1000))
QQ_NEW = str(300000000 + int(RUN[-8:]) % 90000000)
QQ_ROTATE = str(QQ_NEW) + "1"
QQ_BRUTE = str(QQ_NEW) + "2"

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


def user_headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def ask_code(qq: str) -> str:
    r = c.post(f"{API}/verify/request", json={"qq": qq}, headers=H)
    assert r.status_code == 200, r.text
    return r.json()["code"]


def new_invite(max_uses: int = 1) -> str:
    r = c.post(f"{API}/invites", json={"note": f"test-{RUN}", "max_uses": max_uses}, headers=H)
    assert r.status_code == 200, r.text
    return r.json()["code"]


print(f"→ {API}  RUN={RUN}  新用户 QQ={QQ_NEW}\n")

# ---------------------------------------------------------------------------
print("--- 1. 两种令牌的范围 ---")
r = c.get(f"{API}/health")
check("无令牌 → 401", r.status_code, 401)

r = c.get(f"{API}/health", headers={"Authorization": "Bearer nonsense-token"})
check("坏令牌 → 401", r.status_code, 401)

r = c.get(f"{API}/health", headers=H)
check("服务令牌读 → 200", r.status_code, 200)

r = c.get(f"{API}/me", headers=H)
check("服务令牌 /api/me → 200", r.status_code, 200)
check("服务令牌的 scope", r.json().get("scope"), "service")

# 签发验证码是**服务令牌专属**：否则谁都能刷新别人的码
r = c.post(f"{API}/verify/request", json={"qq": QQ_NEW}, headers={"Authorization": "Bearer x"})
check("坏令牌签发验证码 → 401", r.status_code, 401)

# ---------------------------------------------------------------------------
print("\n--- 2. 注册需要一个可用的邀请码 ---")
code = ask_code(QQ_NEW)
check_true("服务令牌能签发验证码", len(code) == 6 and code.isdigit(), code)

r = c.post(f"{API}/register", json={"qq": QQ_NEW, "code": code, "invite_code": "不存在的码"})
check("邀请码无效 → 400", r.status_code, 400)
check_true("报错说清了是邀请码的问题", "邀请码" in r.text, r.text[:120])

# 上一步失败**不该消耗掉验证码** —— 用户不该因为填错邀请码而重新去 QQ 要码
r = c.post(f"{API}/register", json={"qq": QQ_NEW, "code": code, "invite_code": new_invite()})
check("同一个验证码还能用来注册 → 200", r.status_code, 200)
body = r.json()
token = body["token"]
check("是新建而不是轮换", body["created"], True)
check_true("令牌有前缀，能一眼认出来", token.startswith("xc_"), token[:12])
check_true("响应里带用户信息", bool(body["user"]), str(body["user"])[:120])
check_true("**响应里不含 token_hash**", "token_hash" not in r.text, r.text[:120])

# ---------------------------------------------------------------------------
print("\n--- 3. 用户令牌能干什么、不能干什么 ---")
r = c.get(f"{API}/me", headers=user_headers(token))
check("用户令牌 /api/me → 200", r.status_code, 200)
check("登录后拿到的是自己", r.json()["user"]["qq"], QQ_NEW)
check("scope 是 user", r.json()["scope"], "user")

# 写接口一律服务令牌专属
r = c.post(f"{API}/messages", json={"message_id": "x", "group_id": "g", "ts": 1}, headers=user_headers(token))
check("用户令牌入库 → 403（不是 401）", r.status_code, 403)
check_true("403 说明了原因", "只允许服务端" in r.text, r.text[:140])

r = c.post(f"{API}/invites", json={"note": "偷发的"}, headers=user_headers(token))
check("用户令牌发邀请码 → 403", r.status_code, 403)

r = c.post(f"{API}/verify/request", json={"qq": QQ_BRUTE}, headers=user_headers(token))
check("用户令牌签发验证码 → 403", r.status_code, 403)

# ---------------------------------------------------------------------------
print("\n--- 4. 一次性语义 ---")
r = c.post(f"{API}/register", json={"qq": QQ_NEW, "code": code, "invite_code": new_invite()})
check("验证码用过即废 → 401", r.status_code, 401)

inv = new_invite(max_uses=1)
code_a = ask_code(QQ_BRUTE)
r = c.post(f"{API}/register", json={"qq": QQ_BRUTE, "code": code_a, "invite_code": inv})
check("用掉邀请码的第一次 → 200", r.status_code, 200)

other_qq = str(int(QQ_BRUTE) + 500)
code_b = ask_code(other_qq)
r = c.post(f"{API}/register", json={"qq": other_qq, "code": code_b, "invite_code": inv})
check("同一个邀请码第二次 → 400（用完了）", r.status_code, 400)

# ---------------------------------------------------------------------------
print("\n--- 5. 防暴力猜验证码 ---")
QQ_GUESS = str(int(QQ_BRUTE) + 900)
real = ask_code(QQ_GUESS)
wrong = "000000" if real != "000000" else "111111"
statuses = []
for _ in range(6):
    r = c.post(f"{API}/register", json={"qq": QQ_GUESS, "code": wrong, "invite_code": new_invite()})
    statuses.append(r.status_code)
check_true("猜错一律 401", all(s == 401 for s in statuses), str(statuses))

r = c.post(f"{API}/register", json={"qq": QQ_GUESS, "code": real, "invite_code": new_invite()})
check("超过尝试上限后，**连对的验证码也不认** → 401", r.status_code, 401)

# ---------------------------------------------------------------------------
print("\n--- 6. 轮换令牌（丢了令牌的唯一出路） ---")
first = token
code_r = ask_code(QQ_NEW)
r = c.post(f"{API}/register", json={"qq": QQ_NEW, "code": code_r})
check("已有用户再走一次流程 → 200（不需要邀请码）", r.status_code, 200)
check("created=False 表示轮换而非新建", r.json()["created"], False)
second = r.json()["token"]
check_true("拿到的是**不同**的令牌", second != first)

check("新令牌可用", c.get(f"{API}/me", headers=user_headers(second)).status_code, 200)
check("旧令牌立刻失效 → 401", c.get(f"{API}/me", headers=user_headers(first)).status_code, 401)

# ---------------------------------------------------------------------------
print("\n--- 7. 服务令牌的归属规则 ---")
r = c.get(f"{API}/users", headers=H)
check("服务令牌能列用户 → 200", r.status_code, 200)
check_true("用户列表里有刚注册的", any(u["qq"] == QQ_NEW for u in r.json()["users"]))
check_true("用户列表里**不含令牌摘要**", "token_hash" not in r.text, r.text[:160])

r = c.get(f"{API}/invites", headers=H)
check("服务令牌能列邀请码 → 200", r.status_code, 200)

r = c.get(f"{API}/users", headers=user_headers(second))
check("用户令牌列全部用户 → 403", r.status_code, 403)

print()
if fails:
    print(f"❌ {len(fails)}/{total} 条失败：")
    for name in fails:
        print(f"   - {name}")
    sys.exit(1)
print(f"✅ {total} 条断言全部通过")
