"""鉴权范围（scope）与附件签名 URL 的回归测试。

需要一个**正在运行的后端**，且两个令牌**不同**：

    # 终端 1（先确认 8003 没被占用）
    $env:API_TOKEN='write-token'; $env:WEB_API_TOKEN='web-token'
    $env:DB_PATH='data/scope.db'; $env:ATTACHMENT_DIR='data/scope-att'
    $env:SERVER_PORT='8003'; .\\.venv\\Scripts\\python.exe -m app.main

    # 终端 2
    $env:SCOPE_BASE='http://127.0.0.1:8003'
    $env:SCOPE_WRITE_TOKEN='write-token'; $env:SCOPE_WEB_TOKEN='web-token'
    .\\.venv\\Scripts\\python.exe -m tests.check_auth_scopes

覆盖：
  - 网页令牌**读得到、写不了**（写接口返回 403 而不是 401 —— 身份有效但没权限）
  - 网页令牌仍然能提交人工修正、标记已读（这两个是刻意放开的）
  - 写入令牌（bot）全部照常
  - 附件签名 URL：不带任何头也能取到；改签名、改 id、过期都拒绝；
    带 Bearer 不带签名仍然可以

「过期」这一条是**真的**在测过期逻辑，不是测签名不匹配：测试自己按
signing.py 的算法复现密钥，签一个 exp 在过去的 URL。否则把 exp 改掉会同时
让签名失效，就分不清到底拒绝的是哪一个原因。

每次运行用新的 RUN 后缀造数据，重复运行是安全的。
"""

from __future__ import annotations

import hashlib
import hmac
import os
import sys
import time

import httpx

BASE = os.environ.get("SCOPE_BASE", "http://127.0.0.1:8003").rstrip("/")
API = BASE + "/api"
WRITE = os.environ.get("SCOPE_WRITE_TOKEN", "write-token")
WEB = os.environ.get("SCOPE_WEB_TOKEN", "web-token")

RUN = os.environ.get("SCOPE_RUN") or str(int(time.time() * 1000))
GROUP = f"scope-g-{RUN}"

WH = {"Authorization": f"Bearer {WRITE}"}
WE = {"Authorization": f"Bearer {WEB}"}

# 1x1 的合法 PNG
PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
    "890000000a49444154789c63000100000500010d0a2db40000000049454e44ae426082"
)

c = httpx.Client(timeout=20)
fails: list[str] = []
total = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global total
    total += 1
    print(("ok    " if cond else "FAIL  ") + name + (f"  {detail}" if detail else ""))
    if not cond:
        fails.append(name)


def now_ms() -> int:
    return int(time.time() * 1000)


# ---------------------------------------------------------------------------
# signing.py 的算法在这里复现一份 —— 为了能签"合法但已过期"的 URL。
# 密钥 = HMAC(ctx, API_TOKEN)，与 signing._key() 一致。
# ---------------------------------------------------------------------------
_CTX = b"xcollector-attachment-url-v1"


def _key() -> bytes:
    return hmac.new(_CTX, WRITE.encode("utf-8"), hashlib.sha256).digest()


def forge(att_id: str, exp: int, *, key: bytes | None = None) -> str:
    k = _key() if key is None else key
    sig = hmac.new(k, f"{att_id}.{exp}".encode("utf-8"), hashlib.sha256).hexdigest()[:32]
    return f"{API}/attachments/{att_id}?exp={exp}&sig={sig}"


print(f"→ {API}  RUN={RUN}\n")

# ---------------------------------------------------------------------------
print("--- 0. 前置：两个令牌必须不同，否则这个测试没意义 ---")
check("WEB_API_TOKEN 与 API_TOKEN 不同", WRITE != WEB, f"{WRITE!r} vs {WEB!r}")

# ---------------------------------------------------------------------------
print("\n--- 1. 认证基础 ---")
r = c.get(f"{API}/health")
check("无令牌 → 401", r.status_code == 401, f"HTTP {r.status_code}")
r = c.get(f"{API}/health", headers={"Authorization": "Bearer nope"})
check("错令牌 → 401", r.status_code == 401, f"HTTP {r.status_code}")
r = c.get(f"{API}/health", headers=WH)
check("写入令牌读 → 200", r.status_code == 200, f"HTTP {r.status_code}")
r = c.get(f"{API}/health", headers=WE)
check("网页令牌读 → 200", r.status_code == 200, f"HTTP {r.status_code}")

# ---------------------------------------------------------------------------
print("\n--- 2. 网页令牌写不了（403，不是 401）---")
ts = now_ms()

WRITE_ONLY: list[tuple[str, str, dict | None]] = [
    ("POST", "/messages", {"message_id": f"m-{RUN}", "group_id": GROUP, "ts": ts, "content": "x"}),
    ("PATCH", f"/messages/no-such-{RUN}", {"state": "pending"}),
    ("POST", "/notifications", {}),
    ("PATCH", f"/notifications/no-such-{RUN}", {"title": "x"}),
    ("DELETE", f"/notifications/no-such-{RUN}", None),
    ("POST", "/groups", {"group_id": GROUP, "last_msg_ts": ts}),
    ("POST", "/gap-alerts", {"group_id": GROUP, "gap_start_ts": ts, "gap_end_ts": ts + 1000}),
    ("POST", f"/gap-alerts/nope-{RUN}/ack", None),
    ("POST", "/stats", {"day": "2026-09-16", "ingested": 1}),
    ("POST", "/digest-log", {"day": "2026-09-16", "kind": "manual", "sent": True}),
    ("PUT", f"/state/ns-{RUN}/k", {"value": {"a": 1}}),
    ("DELETE", f"/state/ns-{RUN}/k", None),
]

for method, path, body in WRITE_ONLY:
    r = c.request(method, API + path, headers=WE, json=body) if body is not None else c.request(
        method, API + path, headers=WE
    )
    ok = r.status_code == 403
    detail = ""
    if not ok:
        detail = f"HTTP {r.status_code}（期望 403）"
    else:
        detail = (r.json() or {}).get("detail", "")
        ok = "只允许 bot" in detail
        if not ok:
            detail = f"403 但 detail 不明确：{detail!r}"
    check(f"网页令牌 {method} {path} → 403", ok, detail)

# 附件上传也要挡住（multipart，单独测）
r = c.post(f"{API}/attachments", headers=WE, files={"file": ("a.png", PNG, "image/png")})
check("网页令牌 POST /attachments → 403", r.status_code == 403, f"HTTP {r.status_code}")

# ---------------------------------------------------------------------------
print("\n--- 3. 用写入令牌建数据（bot 该做的事）---")
r = c.post(
    f"{API}/messages",
    headers=WH,
    json={
        "message_id": f"m-{RUN}",
        "group_id": GROUP,
        "group_name": "示例通知群",
        "sender_id": "10001",
        "sender_name": "张老师",
        "ts": ts,
        "content": "本体：本周五19:00在教三201开班会",
        "attachments": [],
        "raw": {"run": RUN},
    },
)
check("写入令牌 POST /messages → 2xx", r.status_code in (200, 201), f"HTTP {r.status_code}")
raw_id = (r.json() or {}).get("id") or (r.json() or {}).get("raw_id")
check("拿到 raw_id", bool(raw_id), repr(raw_id))

r = c.post(
    f"{API}/notifications",
    headers=WH,
    json={
        "raw_message_id": raw_id,
        "group_id": GROUP,
        "group_name": "示例通知群",
        "source_ts": ts,
        "title": f"范围测试-{RUN}",
        "summary": "摘要",
        "due_at": ts + 3600_000,
        "due_text": "本周五19:00",
        "due_confidence": 0.9,
        "evidence": "本周五19:00在教三201开班会",
        "extractor": "rule",
        "model": "rule-engine",
        "prompt_ver": "llm-v2",
    },
)
check("写入令牌 POST /notifications → 2xx", r.status_code in (200, 201), f"HTTP {r.status_code}")
notif_id = (r.json() or {}).get("id")
check("拿到 notif_id", bool(notif_id), repr(notif_id))

# ---------------------------------------------------------------------------
print("\n--- 4. 网页令牌**能**做的两件事 ---")
r = c.post(
    f"{API}/notifications/{notif_id}/corrections",
    headers=WE,
    json={"field": "location", "value": "教三202", "user_id": "web"},
)
check("网页令牌提交人工修正 → 200", r.status_code == 200, f"HTTP {r.status_code} {r.text[:120]}")

r = c.get(f"{API}/notifications/{notif_id}", headers=WE)
check("网页令牌读详情 → 200", r.status_code == 200, f"HTTP {r.status_code}")
view = (r.json() or {}).get("notification") or {}
check("人工修正已生效（location=教三202）", view.get("location") == "教三202", repr(view.get("location")))
check("manually_edited 已置位", view.get("manually_edited") is True, repr(view.get("manually_edited")))

r = c.post(f"{API}/notifications/{notif_id}/read", headers=WE, json={"read": True})
check("网页令牌标记已读 → 200", r.status_code == 200, f"HTTP {r.status_code}")

# ---------------------------------------------------------------------------
print("\n--- 5. 写入令牌不受影响 ---")
r = c.post(f"{API}/stats", headers=WH, json={"day": "2026-09-16", "ingested": 1})
check("写入令牌 POST /stats → 2xx", r.status_code in (200, 201), f"HTTP {r.status_code}")
r = c.post(f"{API}/notifications/{notif_id}/corrections", headers=WH, json={"field": "status", "value": "done"})
check("写入令牌也能提交修正 → 200", r.status_code == 200, f"HTTP {r.status_code}")

# ---------------------------------------------------------------------------
print("\n--- 6. 附件：上传、签名 URL、不加任何头也能取 ---")
r = c.post(
    f"{API}/attachments",
    headers=WH,
    files={"file": ("证据.png", PNG, "image/png")},
    data={"filename": "证据.png", "source_url": "http://example.com/证据.png"},
)
check("写入令牌上传附件 → 2xx", r.status_code in (200, 201), f"HTTP {r.status_code}")
att = r.json() or {}
att_id = att.get("id")
check("拿到 att_id", bool(att_id), repr(att_id))
check("上传响应里的 url 是**裸路径**（不带 exp/sig）", "sig=" not in str(att.get("url")), repr(att.get("url")))

# 把附件挂到消息上，再让读投影去签
r = c.patch(
    f"{API}/messages/{raw_id}",
    headers=WH,
    json={"attachments": [{"id": att_id, "url": att.get("url"), "type": "image", "name": "证据.png", "size": len(PNG)}]},
)
check("写入令牌 PATCH 消息挂附件 → 2xx", r.status_code in (200, 201), f"HTTP {r.status_code}")

r = c.get(f"{API}/notifications/{notif_id}", headers=WE)
view = (r.json() or {}).get("notification") or {}
atts = view.get("attachments") or []
check("读投影里有 1 个附件", len(atts) == 1, f"实际 {len(atts)} 个")
signed_url = (atts[0] or {}).get("url") if atts else ""
check("读投影里的 url 非空", bool(signed_url), repr(signed_url))
check("读投影里的 url 带 exp", "exp=" in str(signed_url), repr(signed_url))
check("读投影里的 url 带 sig", "sig=" in str(signed_url), repr(signed_url))
check("读投影里的 url 指向同一个附件", str(att_id) in str(signed_url), repr(signed_url))

# 关键：**不带任何 Authorization 头**取签名 URL（浏览器 <img> 就是这么发的）
# 先确认拼出来的是附件地址而不是空串 —— 空串会打到根路径并 200，变成假通过。
check("拼出的请求地址确实指向附件", "/attachments/" in signed_url, repr(signed_url))
full = BASE + str(signed_url)
r = c.get(full)
check("签名 URL 不带任何头 → 200", r.status_code == 200, f"HTTP {r.status_code}")
check("取到的是原字节", r.content == PNG, f"{len(r.content)} 字节")
check("Content-Type 正确", r.headers.get("content-type", "").startswith("image/png"), r.headers.get("content-type"))

# 带 Bearer、不带签名，也应该可以（bot / curl 调试）
r = c.get(f"{API}/attachments/{att_id}", headers=WH)
check("带 Bearer 不带签名 → 200", r.status_code == 200, f"HTTP {r.status_code}")

# 什么都不带 → 401
r = c.get(f"{API}/attachments/{att_id}")
check("什么都不带 → 401", r.status_code == 401, f"HTTP {r.status_code}")

# ---------------------------------------------------------------------------
print("\n--- 7. 签名 URL 的拒绝路径 ---")
r = c.get(f"{API}/attachments/{att_id}?exp=9999999999&sig={'0' * 32}")
check("错签名 → 401", r.status_code == 401, f"HTTP {r.status_code}")

r = c.get(f"{API}/attachments/{att_id}?exp=abc&sig=abc")
check("非数字 exp → 401", r.status_code == 401, f"HTTP {r.status_code}")

r = c.get(f"{API}/attachments/{att_id}?exp=9999999999")
check("只有 exp 没有 sig → 401", r.status_code == 401, f"HTTP {r.status_code}")

# 过期：用真密钥签一个 exp 在过去的 URL —— 签名是对的，只有时间不对
past = int(time.time()) - 10
r = c.get(forge(str(att_id), past))
check("签名正确但已过期 → 401（真的在测过期，不是测签名）", r.status_code == 401, f"HTTP {r.status_code}")

# 把有效期内的签名挪到另一个附件 id 上 → 签名覆盖了 id，必须拒绝
r = c.post(f"{API}/attachments", headers=WH, files={"file": ("b.png", PNG, "image/png")},
           data={"filename": "b.png"})
att2 = (r.json() or {}).get("id")
future = int(time.time()) + 3600
sig_for_1 = forge(str(att_id), future).split("sig=", 1)[1]
r = c.get(f"{API}/attachments/{att2}?exp={future}&sig={sig_for_1}")
check("把 A 的合法签名用在 B 上 → 401（签名覆盖 id）", r.status_code == 401, f"HTTP {r.status_code}")

# 有效期内的自签 URL 应当能取到（证明上面的 401 不是因为整个机制坏了）
r = c.get(forge(str(att_id), future))
check("自签的有效 URL → 200（对照）", r.status_code == 200, f"HTTP {r.status_code}")

# ---------------------------------------------------------------------------
print()
if fails:
    print(f"❌ {len(fails)}/{total} 条失败：")
    for name in fails:
        print(f"   - {name}")
    sys.exit(1)
print(f"✅ {total} 条断言全部通过")
