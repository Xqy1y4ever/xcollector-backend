"""附件签名 URL：不带 Authorization 头也要能取到，且改一点都不行。

需要一个**正在运行的后端**：

    $env:API_TOKEN='service-token'; $env:SERVER_PORT='8004'
    .\\.venv\\Scripts\\python.exe -m app.main

    $env:ATT_BASE='http://127.0.0.1:8004'; $env:ATT_TOKEN='service-token'
    .\\.venv\\Scripts\\python.exe -m tests.check_attachment_urls

为什么单独测：浏览器用 `<img src>` / `<a href>` 取附件，而这两个标签**带不了
Authorization 头**。所以读投影里的附件 url 是现签的短时效链接，下载接口必须
在不看请求头的情况下也能判断"这个链接是不是真的、有没有过期、指向的附件对不对"。
这三件事任何一件错了，要么图裂、要么所有人都能拿别人的附件。

「过期」是**真的**在测过期：测试自己按 signing.py 的算法复现密钥，签一个 exp 在
过去的 URL。否则把 exp 改掉会同时让签名失效，就分不清拒绝的是哪一个原因。
"""

from __future__ import annotations

import hashlib
import hmac
import os
import sys
import time

import httpx

BASE = os.environ.get("ATT_BASE", "http://127.0.0.1:8004").rstrip("/")
API = BASE + "/api"
TOKEN = os.environ.get("ATT_TOKEN", "service-token")

RUN = os.environ.get("ATT_RUN") or str(int(time.time() * 1000))
GROUP = f"att-g-{RUN}"

H = {"Authorization": f"Bearer {TOKEN}"}
PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
    "890000000a49444154789c63000100000500010d0a2db40000000049454e44ae426082"
)

_CTX = b"xcollector-attachment-url-v1"


def _key() -> bytes:
    return hmac.new(_CTX, TOKEN.encode("utf-8"), hashlib.sha256).digest()


def forge(user_id: str, att_id: str, exp: int) -> str:
    """按 signing.py 的算法复现一个签名 URL。

    `user_id` 也在签名内容里（多用户之后必须绑定归属），所以这里必须带上 ——
    这也是本文件能区分"签名不对"和"过期了"的前提。
    """
    sig = hmac.new(
        _key(), f"{user_id}.{att_id}.{exp}".encode("utf-8"), hashlib.sha256
    ).hexdigest()[:32]
    return f"{API}/attachments/{att_id}?exp={exp}&u={user_id}&sig={sig}"


# 多用户之后所有数据都要有归属，服务令牌也必须显式指定 user_id。
# 这里注册一个临时用户，并让整个客户端默认带上它。
_boot = httpx.Client(timeout=20)
_qq = str(400000000 + int(RUN[-8:]) % 50000000)
try:
    _code = _boot.post(f"{API}/verify/request", json={"qq": _qq}, headers=H).json()["code"]
    _inv = _boot.post(
        f"{API}/invites", json={"note": f"att-{RUN}", "max_uses": 1}, headers=H
    ).json()["code"]
    _r = _boot.post(f"{API}/register", json={"qq": _qq, "code": _code, "invite_code": _inv})
    UID = str(_r.json()["user"]["id"])
finally:
    _boot.close()


c = httpx.Client(timeout=20, params={"user_id": UID})

# 取签名 URL **必须**用这个不带默认参数的客户端。
#
# 坑（httpx 0.28.1 实测）：只要 `params` 非空 —— 不管是 client 级的默认值还是
# 调用时传的 —— URL 里内联的那段 query 就被**整个丢掉**，不是合并：
#
#     c = httpx.Client(base_url="http://h", params={"user_id": "U"})
#     c.build_request("GET", "/a?exp=1&sig=S").url   ->  http://h/a?user_id=U
#     c.build_request("GET", "/a?exp=1&sig=S", params={"q": "Q"}).url
#                                                   ->  http://h/a?user_id=U&q=Q
#
# 本项目所有签名 URL 都是"内联 query"形式，所以 `c.get(BASE + signed)` 发出去的
# 其实是 `/api/attachments/att_x?user_id=usr...` —— exp/u/sig 全没了。那样每一条
# 签名断言（连"过期必须拒绝"）都会因为"压根没带签名"而通过，全绿但什么都没验证。
# 下面所有下载接口的请求都走 `sb`，并在末尾用一条对照断言把这件事钉住。
sb = httpx.Client(timeout=20)
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


print(f"→ {API}  RUN={RUN}\n")

# ---------------------------------------------------------------------------
print("--- 准备：上传附件并挂到一条消息上 ---")
ts = int(time.time() * 1000)

r = c.post(
    f"{API}/attachments",
    headers=H,
    files={"file": ("证据.png", PNG, "image/png")},
    data={"filename": "证据.png", "source_url": "http://example.com/证据.png"},
)
check("上传 → 200", r.status_code, 200)
att = r.json()
att_id = att["id"]
check_true("上传响应里的 url 是**裸路径**（签名会过期，存不得）", "sig=" not in str(att["url"]), str(att["url"]))

r = c.post(
    f"{API}/messages",
    headers=H,
    json={"message_id": f"m-{RUN}", "group_id": GROUP, "ts": ts, "content": "本体"},
)
raw_id = r.json()["id"]

r = c.patch(
    f"{API}/messages/{raw_id}",
    headers=H,
    json={"attachments": [{"id": att_id, "url": att["url"], "type": "image", "name": "证据.png"}]},
)
check("挂附件 → 200", r.status_code, 200)

r = c.post(
    f"{API}/notifications",
    headers=H,
    json={
        "raw_message_id": raw_id,
        "group_id": GROUP,
        "sender_id": "10001",
        "source_ts": ts,
        "title": f"附件测试-{RUN}",
        "evidence": "证据",
    },
)
check("建条 → 200", r.status_code, 200)
notif_id = r.json()["id"]

# ---------------------------------------------------------------------------
print("\n--- 读投影里的 url 是签过的 ---")
r = c.get(f"{API}/notifications/{notif_id}", headers=H)
view = (r.json() or {}).get("notification") or {}
atts = view.get("attachments") or []
check("读投影里有 1 个附件", len(atts), 1)
signed = (atts[0] if atts else {}).get("url") or ""
check_true("带 exp", "exp=" in signed, repr(signed))
check_true("带 sig", "sig=" in signed, repr(signed))
check_true("指向同一个附件", att_id in signed, repr(signed))

print("\n--- 不带任何请求头也能取到（浏览器 <img> 就是这么发的）---")
r = sb.get(BASE + signed)
check("签名 URL 不带任何头 → 200", r.status_code, 200)
check("取到的是原字节", r.content, PNG)
check_true("Content-Type 正确", r.headers.get("content-type", "").startswith("image/png"))

print("\n--- 拒绝路径 ---")
r = sb.get(f"{API}/attachments/{att_id}")
check("什么都不带 → 401", r.status_code, 401)

r = sb.get(f"{API}/attachments/{att_id}", headers=H)
check("带 Bearer、不带签名 → 200", r.status_code, 200)

r = sb.get(f"{API}/attachments/{att_id}?exp=9999999999&sig={'0' * 32}")
check("错签名 → 401", r.status_code, 401)

r = sb.get(f"{API}/attachments/{att_id}?exp=abc&sig=abc")
check("非数字 exp → 401", r.status_code, 401)

r = sb.get(f"{API}/attachments/{att_id}?exp=9999999999")
check("只有 exp 没有 sig → 401", r.status_code, 401)

past = int(time.time()) - 10
r = sb.get(forge(UID, att_id, past))
check("签名正确但**已过期** → 401（真的在测过期，不是测签名）", r.status_code, 401)

r = sb.post(f"{API}/attachments", headers=H, files={"file": ("b.png", PNG, "image/png")})
att2 = r.json()["id"]
future = int(time.time()) + 3600
sig_for_1 = forge(UID, att_id, future).split("sig=", 1)[1]
r = sb.get(f"{API}/attachments/{att2}?exp={future}&u={UID}&sig={sig_for_1}")
check("把 A 的合法签名用在 B 上 → 401（签名覆盖 id）", r.status_code, 401)

# 把 u 换成别人：签名内容里含 user_id，所以必须验不过
r = sb.get(forge(UID, att_id, future).replace(f"u={UID}", "u=someone-else"))
check("把 u 换成别的用户 → 401（签名覆盖 user_id）", r.status_code, 401)

r = sb.get(forge(UID, att_id, future))
check("自签的有效 URL → 200（对照，证明机制本身是通的）", r.status_code, 200)

# 对照 1：确认上面那条 200 不是"因为请求里根本没有签名"才通过的。
# 少一个 query 参数就必须掉到 401 —— 否则整段签名测试都是空转。
r = sb.get(f"{API}/attachments/{att_id}?exp={future}&u={UID}")
check("对照：不带 sig 的同一请求 → 401（证明上面确实带着签名）", r.status_code, 401)

print("\n--- 没有归属就不签名（共享层没有 owner 可绑）---")
# GET /api/messages* 是共享层、拿不到 owner。以前那里会签出 `?exp=..&u=&sig=..`，
# 而校验侧要求 u 非空 —— 那条链接**永远 401**，而且看起来完全正常
# （有 exp 有 sig）。现在的行为是诚实地返回裸路径：要靠 Bearer。
r = c.post(
    f"{API}/messages",
    headers=H,
    json={
        "message_id": f"raw-att-{RUN}",
        "group_id": GROUP,
        "ts": ts,
        "content": "共享层的附件",
        "attachments": [{"id": att_id, "url": att["url"], "type": "image"}],
    },
)
raw_id2 = r.json()["id"]
r = c.get(f"{API}/messages/{raw_id2}", headers=H)
shared_atts = (r.json() or {}).get("attachments") or []
shared_url = (shared_atts[0] if shared_atts else {}).get("url") or ""
check("共享层的附件 url 是裸路径", shared_url, att["url"])
check_true("裸路径里没有 u=（不会伪造一条用不了的签名）", "u=" not in shared_url, shared_url)
r = sb.get(BASE + shared_url)
check("裸路径不带任何头 → 401（本来就要靠 Bearer）", r.status_code, 401)
r = sb.get(BASE + shared_url, headers=H)
check("裸路径带 Bearer → 200", r.status_code, 200)

print()
if fails:
    print(f"❌ {len(fails)}/{total} 条失败：")
    for name in fails:
        print(f"   - {name}")
    sys.exit(1)
print(f"✅ {total} 条断言全部通过")
