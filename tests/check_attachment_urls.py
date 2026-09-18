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


def forge(att_id: str, exp: int) -> str:
    sig = hmac.new(_key(), f"{att_id}.{exp}".encode("utf-8"), hashlib.sha256).hexdigest()[:32]
    return f"{API}/attachments/{att_id}?exp={exp}&sig={sig}"


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
r = c.get(BASE + signed)
check("签名 URL 不带任何头 → 200", r.status_code, 200)
check("取到的是原字节", r.content, PNG)
check_true("Content-Type 正确", r.headers.get("content-type", "").startswith("image/png"))

print("\n--- 拒绝路径 ---")
r = c.get(f"{API}/attachments/{att_id}")
check("什么都不带 → 401", r.status_code, 401)

r = c.get(f"{API}/attachments/{att_id}", headers=H)
check("带 Bearer、不带签名 → 200", r.status_code, 200)

r = c.get(f"{API}/attachments/{att_id}?exp=9999999999&sig={'0' * 32}")
check("错签名 → 401", r.status_code, 401)

r = c.get(f"{API}/attachments/{att_id}?exp=abc&sig=abc")
check("非数字 exp → 401", r.status_code, 401)

r = c.get(f"{API}/attachments/{att_id}?exp=9999999999")
check("只有 exp 没有 sig → 401", r.status_code, 401)

past = int(time.time()) - 10
r = c.get(forge(att_id, past))
check("签名正确但**已过期** → 401（真的在测过期，不是测签名）", r.status_code, 401)

r = c.post(f"{API}/attachments", headers=H, files={"file": ("b.png", PNG, "image/png")})
att2 = r.json()["id"]
future = int(time.time()) + 3600
sig_for_1 = forge(att_id, future).split("sig=", 1)[1]
r = c.get(f"{API}/attachments/{att2}?exp={future}&sig={sig_for_1}")
check("把 A 的合法签名用在 B 上 → 401（签名覆盖 id）", r.status_code, 401)

r = c.get(forge(att_id, future))
check("自签的有效 URL → 200（对照，证明机制本身是通的）", r.status_code, 200)

print()
if fails:
    print(f"❌ {len(fails)}/{total} 条失败：")
    for name in fails:
        print(f"   - {name}")
    sys.exit(1)
print(f"✅ {total} 条断言全部通过")
