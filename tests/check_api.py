"""HTTP 接口端到端冒烟测试（按 docs/api.md 的新契约）。

需要一个**正在运行的后端**。推荐用一个独立的库和附件目录，别污染真库：

    # 终端 1（先确认 8001 没被占用：netstat -ano | findstr :8001）
    $env:API_TOKEN='smoke-token'; $env:DB_PATH='data/smoke.db'
    $env:ATTACHMENT_DIR='data/smoke-att'; $env:MEDIA_MAX_BYTES='1048576'
    $env:SERVER_PORT='8001'; .\\.venv\\Scripts\\python.exe -m app.main

    # 终端 2
    $env:SMOKE_BASE='http://127.0.0.1:8001'; $env:SMOKE_TOKEN='smoke-token'
    $env:SMOKE_MEDIA_MAX='1048576'; .\\.venv\\Scripts\\python.exe -m tests.check_api

覆盖（每条都是一次真的 HTTP 请求）：认证（无/错 token → 401）、消息创建幂等与
PATCH 只允许 state/state_reason/attachments（content 传了也改不动）、多值 state 过滤、
通知创建幂等、evidence 为空 → 400、读投影（人工修正覆盖、status 推导、字段齐全）、
PATCH 忽略 status/read、修正历史、已读、since 增量游标、删除、附件上传/下载/413/
目录穿越文件名、群 upsert 的 previous_last_msg_ts、缺口告警 ack、统计累加、
digest-log 幂等与 count_only、bot_state 的 TTL 过期语义与 count_only、health 形状。

每次运行用一个新的 RUN 后缀（时间戳）造数据，所以**重复运行是安全的**。
"""

from __future__ import annotations

import os
import sys
import time
from datetime import datetime

import httpx

BASE = os.environ.get("SMOKE_BASE", "http://127.0.0.1:8001").rstrip("/")
API = BASE + "/api"
TOKEN = os.environ.get("SMOKE_TOKEN", "smoke-token")
MEDIA_MAX = int(os.environ.get("SMOKE_MEDIA_MAX", str(5 * 1024 * 1024)))
RUN = os.environ.get("SMOKE_RUN") or str(int(time.time() * 1000))

GROUP = f"smoke-g-{RUN}"
TITLE_A = f"smokeA-{RUN}"
TITLE_B = f"smokeB-{RUN}"
TITLE_C = f"smokeC-{RUN}"
BOT_STATE = f"state-{RUN}"

H = {"Authorization": f"Bearer {TOKEN}"}
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


def message_body(message_id: str, ts: int, text: str = "正文：冒烟测试") -> dict:
    return {
        "message_id": message_id,
        "group_id": GROUP,
        "group_name": "示例通知群",
        "sender_id": "10001",
        "sender_name": "张老师",
        "ts": ts,
        "content": text,
        "attachments": [],
        "raw": {"smoke": True, "run": RUN},
    }


def notification_body(
    raw_id: str,
    *,
    evidence: str,
    title: str,
    ts: int,
    due_at: int | None = None,
) -> dict:
    return {
        "raw_message_id": raw_id,
        "group_id": GROUP,
        "group_name": "示例通知群",
        "sender_id": "10001",
        "sender_name": "张老师",
        "source_ts": ts,
        "title": title,
        "summary": "摘要",
        "location": "教三201",
        "due_at": due_at,
        "due_text": "下周三前",
        "due_confidence": 0.72,
        "evidence": evidence,
        "conflict": False,
        "candidates": [{"model": "deepseek/deepseek-chat", "due_at": due_at}],
        "extractor": "llm",
        "model": "deepseek/deepseek-chat",
        "prompt_ver": "llm-v2",
    }


def find(notifications: list[dict], nid: str) -> dict | None:
    return next((n for n in notifications if n["id"] == nid), None)


print(f"→ {API}  RUN={RUN}  MEDIA_MAX={MEDIA_MAX}\n")

# ---------------------------------------------------------------------------
# 1. 认证
# ---------------------------------------------------------------------------
print("--- 认证 ---")
r = c.get(f"{API}/health")
check("无 token → 401", r.status_code == 401, f"HTTP {r.status_code}")
check("401 有 detail", bool((r.json() or {}).get("detail")) if r.status_code == 401 else False)
r = c.get(f"{API}/health", headers={"Authorization": "Bearer wrong-token"})
check("错 token → 401", r.status_code == 401, f"HTTP {r.status_code}")
r = c.get(f"{API}/notifications", headers={"Authorization": "Bearer"})
check("空 token → 401", r.status_code == 401, f"HTTP {r.status_code}")
r = c.post(f"{API}/messages", json=message_body(f"noauth-{RUN}", now_ms()))
check("写接口无 token → 401", r.status_code == 401, f"HTTP {r.status_code}")
r = c.get(f"{API}/health", headers=H)
check("正确 token → 200", r.status_code == 200, f"HTTP {r.status_code}")
if r.status_code != 200:
    print("\n服务端没按 API_TOKEN 启动？请按本文件顶部的方式启动以后再跑一次。")
    sys.exit(1)

# ---------------------------------------------------------------------------
# 2. health / 存储
# ---------------------------------------------------------------------------
print("\n--- health ---")
h = r.json()
check("health.ok 为真", h.get("ok") is True, str(h)[:160])
check("storage.driver=sqlite", (h.get("storage") or {}).get("driver") == "sqlite", str(h.get("storage")))
check("storage.path 非空", bool((h.get("storage") or {}).get("path")))
check("storage.writable 为真", (h.get("storage") or {}).get("writable") is True)
counts = h.get("counts") or {}
check("counts 三个键齐全", set(counts) == {"messages", "notifications", "attachments"}, str(counts))
check("counts 都是整数", all(isinstance(v, int) for v in counts.values()), str(counts))
check("version 与代码一致", h.get("version") == __import__("app").__version__, str(h.get("version")))
check("server_time 是毫秒整数", isinstance(h.get("server_time"), int) and h["server_time"] > 1_600_000_000_000)

# ---------------------------------------------------------------------------
# 3. 原始消息：创建 / 幂等 / 过滤 / PATCH
# ---------------------------------------------------------------------------
print("\n--- messages ---")
ts1 = now_ms()
r = c.post(f"{API}/messages", json=message_body(f"m1-{RUN}", ts1), headers=H)
check("POST /messages 200", r.status_code == 200, r.text[:160])
mid1 = (r.json() or {}).get("id")
check("is_new=true", (r.json() or {}).get("is_new") is True, r.text[:120])
check("返回 id 非空", bool(mid1))

r2 = c.post(f"{API}/messages", json=message_body(f"m1-{RUN}", ts1), headers=H)
check("重复提交 → 同 id", (r2.json() or {}).get("id") == mid1, r2.text[:160])
check("重复提交 → is_new=false", (r2.json() or {}).get("is_new") is False)
r = c.get(f"{API}/messages", params={"group_id": GROUP, "count_only": "1"}, headers=H)
check("messages count_only=1（一行）", (r.json() or {}).get("count") == 1, r.text[:120])

mid2 = c.post(f"{API}/messages", json=message_body(f"m2-{RUN}", ts1 + 5), headers=H).json()["id"]
r = c.patch(
    f"{API}/messages/{mid2}",
    json={
        # 这三个允许改
        "state": BOT_STATE,
        "state_reason": "bot 自己定义的原因",
        "attachments": [{"id": "att_demo", "type": "image", "url": "/api/attachments/att_demo"}],
        # 这些一律不可改（传了必须被忽略）
        "content": "被改写的正文",
        "ts": 1,
        "message_id": "hacked",
        "group_id": "hacked-group",
        "sender_id": "hacked-sender",
        "raw": {"hacked": True},
    },
    headers=H,
)
check("PATCH /messages 200", r.status_code == 200, r.text[:200])
row = r.json()
check("state 已改", row.get("state") == BOT_STATE, str(row.get("state")))
check("state_reason 已改", row.get("state_reason") == "bot 自己定义的原因", str(row.get("state_reason")))
check("attachments 事后补齐", row.get("attachments") == [{"id": "att_demo", "type": "image", "url": "/api/attachments/att_demo"}], str(row.get("attachments")))
check("content 传了也没被改", row.get("content") == "正文：冒烟测试", str(row.get("content"))[:60])
check("ts 传了也没被改", row.get("ts") == ts1 + 5, str(row.get("ts")))
check("message_id 传了也没被改", row.get("message_id") == f"m2-{RUN}", str(row.get("message_id")))
check("group_id 传了也没被改", row.get("group_id") == GROUP, str(row.get("group_id")))
check("sender_id 传了也没被改", row.get("sender_id") == "10001", str(row.get("sender_id")))
check("raw 传了也没被改", row.get("raw") == {"smoke": True, "run": RUN}, str(row.get("raw")))

r = c.get(f"{API}/messages/{mid2}", headers=H)
check("GET /messages/{id} 200", r.status_code == 200, r.text[:160])
detail = r.json()
check("行里有 content/raw/attachments/state", all(k in detail for k in ("content", "raw", "attachments", "state")))
check("GET 单条与 PATCH 结果一致", detail.get("content") == "正文：冒烟测试")

r = c.get(f"{API}/messages", params={"group_id": GROUP, "state": f"pending,{BOT_STATE}"}, headers=H)
check("state 逗号分隔多值 → 2 条", len(r.json().get("messages", [])) == 2, r.text[:200])
r = c.get(f"{API}/messages", params={"group_id": GROUP, "state": [BOT_STATE, "pending"]}, headers=H)
check("state 重复参数 → 2 条", len(r.json().get("messages", [])) == 2, r.text[:200])
r = c.get(f"{API}/messages", params={"group_id": GROUP, "state": BOT_STATE, "count_only": "1"}, headers=H)
check("state 单值 count_only → 1", (r.json() or {}).get("count") == 1, r.text[:120])
r = c.get(f"{API}/messages", params={"group_id": GROUP, "state": "不存在的状态", "count_only": "1"}, headers=H)
check("未知 state → 0", (r.json() or {}).get("count") == 0, r.text[:120])
r = c.get(f"{API}/messages", params={"group_id": GROUP, "since": ts1 + 100, "count_only": "1"}, headers=H)
check("since 过滤（ts >= since）", (r.json() or {}).get("count") == 0, r.text[:120])
r = c.get(f"{API}/messages", params={"limit": "1", "group_id": GROUP}, headers=H)
check("limit 生效", len(r.json().get("messages", [])) == 1, r.text[:160])
r = c.get(f"{API}/messages/{mid1}", headers=H)
check("GET 未知 id → 404", c.get(f"{API}/messages/nope-{RUN}", headers=H).status_code == 404)
check("PATCH 未知 id → 404", c.patch(f"{API}/messages/nope-{RUN}", json={"state": "x"}, headers=H).status_code == 404)

# ---------------------------------------------------------------------------
# 4. 通知：创建 / 幂等 / evidence 硬约束
# ---------------------------------------------------------------------------
print("\n--- notifications ---")
ts2 = now_ms()
r = c.post(
    f"{API}/notifications",
    json=notification_body(mid1, evidence=f"依据A {TITLE_A}", title=TITLE_A, ts=ts2, due_at=ts2 + 86_400_000),
    headers=H,
)
check("POST /notifications 200", r.status_code == 200, r.text[:200])
nid_a = (r.json() or {}).get("id")
check("created=true", (r.json() or {}).get("created") is True, r.text[:120])

r = c.post(
    f"{API}/notifications",
    json=notification_body(mid1, evidence=f"依据A {TITLE_A}", title=TITLE_A, ts=ts2, due_at=ts2 + 86_400_000),
    headers=H,
)
check("重复提交 → 同 id", (r.json() or {}).get("id") == nid_a, r.text[:160])
check("重复提交 → created=false", (r.json() or {}).get("created") is False)

r = c.post(
    f"{API}/notifications",
    json=notification_body(mid1, evidence="   ", title="没有证据", ts=ts2),
    headers=H,
)
check("evidence 为空 → 400", r.status_code == 400, f"HTTP {r.status_code} {r.text[:120]}")
check("400 说明「没有证据的条目不许入库」", "没有证据的条目不许入库" in (r.json() or {}).get("detail", ""), r.text[:160])
r = c.post(f"{API}/notifications", json=notification_body(mid1, evidence="", title="没有证据", ts=ts2), headers=H)
check("evidence 空串 → 400", r.status_code == 400, f"HTTP {r.status_code}")
r = c.post(f"{API}/notifications", json=notification_body(mid1, evidence=None, title="没有证据", ts=ts2), headers=H)
check("evidence 为 null → 400", r.status_code == 400, f"HTTP {r.status_code}")
r = c.post(f"{API}/notifications", json=notification_body(mid1, evidence=f"依据A {TITLE_A}", title=TITLE_A, ts=ts2, due_at=ts2 + 86_400_000), headers=H)
check("重复提交不会新增行（同 id）", (r.json() or {}).get("id") == nid_a, r.text[:160])

r = c.post(
    f"{API}/notifications",
    json=notification_body(mid2, evidence=f"依据B {TITLE_B}", title=TITLE_B, ts=ts2 - 1000, due_at=ts2 - 86_400_000),
    headers=H,
)
nid_b = (r.json() or {}).get("id")
check("第二条通知已建", r.status_code == 200 and bool(nid_b), r.text[:160])

r = c.get(f"{API}/notifications", params={"q": TITLE_A}, headers=H)
payload = r.json()
check("列表返回 server_time", isinstance(payload.get("server_time"), int), str(payload.keys()))
lst = payload.get("notifications", [])
view_a = find(lst, nid_a)
check("q 子串命中", view_a is not None, f"命中 {len(lst)} 条")
check("status 按 due_at 推导 = active", (view_a or {}).get("status") == "active", str((view_a or {}).get("status")))
check("attachments 取自 raw_message", (view_a or {}).get("attachments") == [], str((view_a or {}).get("attachments")))
check("candidates 原样透出", isinstance((view_a or {}).get("candidates"), list))
check("manually_edited 初始为 false", (view_a or {}).get("manually_edited") is False)
check("read 初始为 false", (view_a or {}).get("read") is False)
check("evidence 保留原文", (view_a or {}).get("evidence") == f"依据A {TITLE_A}", str((view_a or {}).get("evidence")))
from app.materialize import VIEW_FIELDS  # noqa: E402  （只用来对齐契约字段表）

check("读投影字段与契约完全一致", set(view_a or {}) == set(VIEW_FIELDS), f"缺={set(VIEW_FIELDS) - set(view_a or {})} 多={set(view_a or {}) - set(VIEW_FIELDS)}")

r = c.get(f"{API}/notifications", params={"q": TITLE_B}, headers=H)
check("过期通知 status=expired", (find(r.json()["notifications"], nid_b) or {}).get("status") == "expired", r.text[:200])
r = c.get(f"{API}/notifications", params={"status": "active", "q": RUN}, headers=H)
check("?status=active 只留未过期", [n["id"] for n in r.json()["notifications"]] == [nid_a], r.text[:200])
r = c.get(f"{API}/notifications", params={"status": "expired", "q": RUN}, headers=H)
check("?status=expired 命中过期那条", [n["id"] for n in r.json()["notifications"]] == [nid_b], r.text[:200])
r = c.get(f"{API}/notifications", params={"q": RUN, "count_only": "1"}, headers=H)
check("count_only=1 只返回 count", set((r.json() or {}).keys()) == {"count"}, r.text[:160])
check("count_only 计数正确（2 条）", (r.json() or {}).get("count") == 2, r.text[:120])
r = c.get(f"{API}/notifications", params={"status": "active", "q": RUN, "count_only": "1"}, headers=H)
check("status + count_only 组合", (r.json() or {}).get("count") == 1, r.text[:120])
r = c.get(f"{API}/notifications", params={"q": "绝不存在的关键词-" + RUN}, headers=H)
check("q 无命中 → 空列表", r.json().get("notifications") == [], r.text[:160])

# ---------------------------------------------------------------------------
# 5. 人工修正 + 读投影 + status 优先级
# ---------------------------------------------------------------------------
print("\n--- corrections ---")
r = c.post(
    f"{API}/notifications/{nid_a}/corrections",
    json={"field": "due_at", "value": ts2 - 60_000, "user_id": "smoke"},
    headers=H,
)
check("修正 due_at 200", r.status_code == 200, r.text[:200])
check("修正后 due_at 生效", (r.json() or {}).get("notification", {}).get("due_at") == ts2 - 60_000, r.text[:200])
check("修正后 status 推导为 expired", (r.json() or {}).get("notification", {}).get("status") == "expired", r.text[:200])
check("manually_edited=true", (r.json() or {}).get("notification", {}).get("manually_edited") is True)

r = c.post(
    f"{API}/notifications/{nid_a}/corrections",
    json={"field": "status", "value": "done", "user_id": "smoke"},
    headers=H,
)
check("status 人工修正优先", (r.json() or {}).get("notification", {}).get("status") == "done", r.text[:200])
r = c.post(
    f"{API}/notifications/{nid_a}/corrections",
    json={"field": "title", "value": "人工改过的标题", "user_id": "smoke"},
    headers=H,
)
check("title 人工修正覆盖", (r.json() or {}).get("notification", {}).get("title") == "人工改过的标题", r.text[:200])
r = c.post(
    f"{API}/notifications/{nid_a}/corrections",
    json={"field": "location", "value": "", "user_id": "smoke"},
    headers=H,
)
check("location 可以被人工清空", (r.json() or {}).get("notification", {}).get("location") is None, r.text[:200])
r = c.get(f"{API}/notifications", params={"status": "done", "q": RUN}, headers=H)
check("?status=done 能筛到（修正优先于推导）", [n["id"] for n in r.json()["notifications"]] == [nid_a], r.text[:200])
r = c.get(f"{API}/notifications", params={"q": RUN}, headers=H)
check("列表里的也是修正后的值", (find(r.json()["notifications"], nid_a) or {}).get("title") == "人工改过的标题", r.text[:200])

r = c.post(f"{API}/notifications/{nid_a}/corrections", json={"field": "due_text", "value": "本周五前"}, headers=H)
check("due_text 可修正", r.status_code == 200 and r.json()["notification"]["due_text"] == "本周五前", r.text[:200])
r = c.post(f"{API}/notifications/{nid_a}/corrections", json={"field": "evidence", "value": "想改证据"}, headers=H)
check("不可修正字段（evidence）→ 400", r.status_code == 400, f"HTTP {r.status_code}")
r = c.post(f"{API}/notifications/{nid_a}/corrections", json={"field": "status", "value": "not-a-status"}, headers=H)
check("status 非法值 → 400", r.status_code == 400, f"HTTP {r.status_code}")
r = c.post(f"{API}/notifications/{nid_a}/corrections", json={"field": "due_at", "value": "不是时间"}, headers=H)
check("due_at 非数字 → 400", r.status_code == 400, f"HTTP {r.status_code}")
r = c.post(f"{API}/notifications/nope-{RUN}/corrections", json={"field": "title", "value": "x"}, headers=H)
check("修正不存在的通知 → 404", r.status_code == 404, f"HTTP {r.status_code}")

r = c.get(f"{API}/notifications/{nid_a}/corrections", headers=H)
corr = r.json().get("corrections", [])
check("修正历史按时间正序（5 条）", [x["field"] for x in corr] == ["due_at", "status", "title", "location", "due_text"], str([x["field"] for x in corr]))
check("修正历史带 user_id/ts", corr and corr[0]["user_id"] == "smoke" and isinstance(corr[0]["ts"], int), str(corr[:1])[:200])

# ---------------------------------------------------------------------------
# 6. PATCH /notifications：允许改机器字段，忽略 status / read
# ---------------------------------------------------------------------------
print("\n--- PATCH /notifications ---")
r = c.patch(
    f"{API}/notifications/{nid_b}",
    json={
        "title": "机器改标题",
        "summary": "机器改摘要",
        "location": "教三301",
        "due_text": "本周五",
        "due_confidence": 0.9,
        "conflict": True,
        "candidates": [{"model": "m2"}],
        "model": "m2",
        "prompt_ver": "v3",
        "evidence": "重跑后的依据",
        # 这两个必须被忽略
        "status": "archived",
        "read": True,
    },
    headers=H,
)
check("PATCH /notifications 200", r.status_code == 200, r.text[:200])
patched = r.json()
check("title 已改", patched.get("title") == "机器改标题")
check("summary 已改", patched.get("summary") == "机器改摘要")
check("location 已改", patched.get("location") == "教三301")
check("due_confidence 已改", patched.get("due_confidence") == 0.9, str(patched.get("due_confidence")))
check("conflict 已改", patched.get("conflict") is True)
check("candidates 已改", patched.get("candidates") == [{"model": "m2"}], str(patched.get("candidates")))
check("model/prompt_ver 已改", patched.get("model") == "m2" and patched.get("prompt_ver") == "v3")
check("evidence 已改", patched.get("evidence") == "重跑后的依据")
check("status 被忽略（仍是 expired）", patched.get("status") == "expired", str(patched.get("status")))
check("read 被忽略（仍是 false）", patched.get("read") is False, str(patched.get("read")))
r = c.patch(f"{API}/notifications/{nid_b}", json={"status": "archived"}, headers=H)
check("PATCH 只传 status 也改不动", r.json().get("status") == "expired", r.text[:200])
r = c.patch(f"{API}/notifications/{nid_b}", json={"evidence": "  "}, headers=H)
check("PATCH 把 evidence 改成空 → 400", r.status_code == 400, f"HTTP {r.status_code}")
check("PATCH 未知 id → 404", c.patch(f"{API}/notifications/nope-{RUN}", json={"title": "x"}, headers=H).status_code == 404)

# ---------------------------------------------------------------------------
# 7. 详情 / 已读 / since 增量 / 删除
# ---------------------------------------------------------------------------
print("\n--- detail / read / since / delete ---")
r = c.get(f"{API}/notifications/{nid_a}", headers=H)
body = r.json()
check("GET /notifications/{id} → notification + raw", set(body.keys()) == {"notification", "raw"}, str(body.keys()))
check("详情里 raw.content 在", (body.get("raw") or {}).get("content") == "正文：冒烟测试", str(body.get("raw"))[:120])
check("详情里 raw.state 在", (body.get("raw") or {}).get("state") == "pending", str((body.get("raw") or {}).get("state")))
check("GET 未知通知 → 404", c.get(f"{API}/notifications/nope-{RUN}", headers=H).status_code == 404)

before_read = now_ms()
time.sleep(0.01)
r = c.post(f"{API}/notifications/{nid_b}/read", json={"read": True}, headers=H)
check("POST /read → {read:true}", r.status_code == 200 and r.json().get("read") is True, r.text[:120])
r = c.get(f"{API}/notifications", params={"q": "机器改标题"}, headers=H)
check("列表里 read=true", (find(r.json()["notifications"], nid_b) or {}).get("read") is True, r.text[:200])
r = c.get(f"{API}/notifications", params={"since": before_read, "q": "机器改标题"}, headers=H)
check("since 增量：已读也算变化", nid_b in [n["id"] for n in r.json()["notifications"]], r.text[:200])
r = c.post(f"{API}/notifications/{nid_b}/read", json={"read": False}, headers=H)
check("取消已读 → {read:false}", r.json().get("read") is False, r.text[:120])

r = c.get(f"{API}/notifications", params={"q": TITLE_C}, headers=H)
cursor = r.json()["server_time"]
time.sleep(0.01)
nid_c = c.post(
    f"{API}/notifications",
    json=notification_body(mid2, evidence=f"依据C {TITLE_C}", title=TITLE_C, ts=now_ms()),
    headers=H,
).json()["id"]
r = c.get(f"{API}/notifications", params={"since": cursor, "q": TITLE_C}, headers=H)
check("since 只返回更新过的行", [n["id"] for n in r.json()["notifications"]] == [nid_c], r.text[:200])

r = c.delete(f"{API}/notifications/{nid_c}", headers=H)
check("DELETE → deleted=true", r.status_code == 200 and r.json().get("deleted") is True, r.text[:160])
check("删除后 GET → 404", c.get(f"{API}/notifications/{nid_c}", headers=H).status_code == 404)
check("重复 DELETE → 404", c.delete(f"{API}/notifications/{nid_c}", headers=H).status_code == 404)
r = c.get(f"{API}/notifications", params={"since": cursor, "q": TITLE_C}, headers=H)
check("删除后不在增量里", r.json()["notifications"] == [], r.text[:160])

# ---------------------------------------------------------------------------
# 8. 附件：上传 / 下载 / 413 / 文件名防穿越
# ---------------------------------------------------------------------------
print("\n--- attachments ---")
png = b"\x89PNG\r\n\x1a\n" + bytes(range(256)) * 4
r = c.post(
    f"{API}/attachments",
    files={"file": ("../../穿越.png", png, "image/png")},
    data={"source_url": "https://cdn.example/x.png"},
    headers=H,
)
check("POST /attachments 200", r.status_code == 200, r.text[:200])
att = r.json()
check("返回 id / url / size / content_type", set(att) == {"id", "url", "size", "content_type"}, str(att))
check("id 形如 att_xxx", str(att.get("id", "")).startswith("att_"), str(att.get("id")))
check("url 形如 /api/attachments/{id}", att.get("url") == f"/api/attachments/{att.get('id')}", str(att.get("url")))
check("size 与上传字节一致", att.get("size") == len(png), f"{att.get('size')} vs {len(png)}")
check("content_type 正确", att.get("content_type") == "image/png", str(att.get("content_type")))

r = c.get(BASE + att["url"], headers=H)
check("GET /attachments/{id} 200", r.status_code == 200, f"HTTP {r.status_code}")
check("下载字节完全一致", r.content == png, f"{len(r.content)} vs {len(png)}")
check("Content-Type 正确", r.headers.get("content-type") == "image/png", r.headers.get("content-type", ""))
check("带 Content-Disposition", "filename" in r.headers.get("content-disposition", ""), r.headers.get("content-disposition", ""))
check("未知附件 → 404", c.get(f"{API}/attachments/att_nope-{RUN}", headers=H).status_code == 404)

from app.attachments import safe_filename  # noqa: E402  （纯函数自检，不碰库）

check("safe_filename 去掉 ../", safe_filename("../../evil.png") == "evil.png", str(safe_filename("../../evil.png")))
check("safe_filename 去掉 ..\\", safe_filename("..\\..\\evil.txt") == "evil.txt", str(safe_filename("..\\..\\evil.txt")))
check("safe_filename 处理中文", safe_filename("中文 文件.pdf") == "中文 文件.pdf", str(safe_filename("中文 文件.pdf")))
check("safe_filename 处理纯 ..", safe_filename("..") is None, str(safe_filename("..")))

big = b"x" * (MEDIA_MAX + 1024)
r = c.post(f"{API}/attachments", files={"file": ("big.bin", big, "application/octet-stream")}, headers=H)
check("超过 MEDIA_MAX_BYTES → 413", r.status_code == 413, f"HTTP {r.status_code} {r.text[:120]}")
r = c.post(f"{API}/attachments", content=b"not multipart", headers={**H, "Content-Type": "application/octet-stream"})
check("非 multipart → 400", r.status_code == 400, f"HTTP {r.status_code}")

# ---------------------------------------------------------------------------
# 9. 群状态 / 缺口告警
# ---------------------------------------------------------------------------
print("\n--- groups / gap-alerts ---")
t0 = now_ms()
r = c.post(f"{API}/groups", json={"group_id": GROUP, "group_name": "示例通知群", "last_msg_ts": t0}, headers=H)
check("POST /groups 200", r.status_code == 200, r.text[:200])
first = r.json()
check("首次 upsert：previous_last_msg_ts=null", first.get("previous_last_msg_ts") is None, r.text[:200])
check("响应带 group 行", set((first.get("group") or {})) >= {"group_id", "group_name", "last_msg_ts", "msg_count_today", "count_date"}, str(first))
check("group.last_msg_ts 已写入", (first.get("group") or {}).get("last_msg_ts") == t0)
r = c.post(f"{API}/groups", json={"group_id": GROUP, "group_name": "示例通知群", "last_msg_ts": t0 + 60_000}, headers=H)
second = r.json()
check("二次 upsert：previous=上一条", second.get("previous_last_msg_ts") == t0, r.text[:200])
check("二次 upsert：last_msg_ts 更新", (second.get("group") or {}).get("last_msg_ts") == t0 + 60_000)
r = c.post(f"{API}/groups", json={"group_id": GROUP, "group_name": None, "last_msg_ts": t0 - 60_000}, headers=H)
third = r.json()
check("乱序旧消息不倒退 last_msg_ts", (third.get("group") or {}).get("last_msg_ts") == t0 + 60_000, r.text[:200])
check("乱序时 previous 仍是更新前的值", third.get("previous_last_msg_ts") == t0 + 60_000, r.text[:200])
r = c.get(f"{API}/groups", headers=H)
check("GET /groups 含本群", any(g["group_id"] == GROUP for g in r.json().get("groups", [])), r.text[:160])

r = c.post(
    f"{API}/gap-alerts",
    json={"group_id": GROUP, "group_name": "示例通知群", "from_ts": t0, "to_ts": t0 + 1000, "reason": f"间隔 16.7 小时 {RUN}"},
    headers=H,
)
check("POST /gap-alerts 200", r.status_code == 200, r.text[:160])
gap_id = (r.json() or {}).get("id")
check("gap id 形如 gap_xxx", str(gap_id).startswith("gap_"), str(gap_id))
r = c.get(f"{API}/gap-alerts", params={"acknowledged": "false", "limit": "50"}, headers=H)
check("未确认列表含它", any(a["id"] == gap_id for a in r.json().get("alerts", [])), r.text[:200])
check("alerts 的 acknowledged 是 bool", all(isinstance(a.get("acknowledged"), bool) for a in r.json().get("alerts", [])))
r = c.post(f"{API}/gap-alerts/{gap_id}/ack", headers=H)
check("ack → acknowledged=true", r.status_code == 200 and r.json().get("acknowledged") is True, r.text[:120])
r = c.get(f"{API}/gap-alerts", params={"acknowledged": "false", "limit": "50"}, headers=H)
check("已确认后不在未确认列表", not any(a["id"] == gap_id for a in r.json().get("alerts", [])), r.text[:200])
r = c.get(f"{API}/gap-alerts", params={"acknowledged": "true", "limit": "50"}, headers=H)
check("已确认列表里有它", any(a["id"] == gap_id for a in r.json().get("alerts", [])), r.text[:200])
check("ack 未知 id → 404", c.post(f"{API}/gap-alerts/gap_nope-{RUN}/ack", headers=H).status_code == 404)
check("acknowledged 非布尔 → 400", c.get(f"{API}/gap-alerts", params={"acknowledged": "maybe"}, headers=H).status_code == 400)

# ---------------------------------------------------------------------------
# 10. 统计累加
# ---------------------------------------------------------------------------
print("\n--- stats ---")
day = datetime.now().strftime("%Y-%m-%d")
r = c.get(f"{API}/stats", params={"day": day}, headers=H)
base = r.json()
check("GET /stats 返回整行", set(base) == {"day", "ingested", "extracted", "unparsed", "conflicts", "degraded", "llm_tokens"}, str(base))
r = c.post(f"{API}/stats", json={"day": day, "fields": {"ingested": 5, "llm_tokens": 100, "未知字段": 1}}, headers=H)
check("POST /stats 累加（+5）", r.json().get("ingested") == base["ingested"] + 5, r.text[:200])
check("未知字段被忽略", "未知字段" not in r.json())
r = c.post(f"{API}/stats", json={"day": day, "fields": {"ingested": 2, "llm_tokens": 42}}, headers=H)
check("再次累加（+2）", r.json().get("ingested") == base["ingested"] + 7, r.text[:200])
check("llm_tokens 也累加", r.json().get("llm_tokens") == base["llm_tokens"] + 142, r.text[:200])
r = c.get(f"{API}/stats", params={"day": day}, headers=H)
check("GET 与 POST 返回一致", r.json().get("ingested") == base["ingested"] + 7, r.text[:200])
r = c.post(f"{API}/stats", json={"fields": {"extracted": 1}}, headers=H)
check("day 省略 → 用服务器当天", r.json().get("day") == day, r.text[:200])

# ---------------------------------------------------------------------------
# 11. digest 发送记录（契约 §10）
# ---------------------------------------------------------------------------
print("\n--- digest-log ---")
kind = f"smoke-{RUN}"
r = c.post(f"{API}/digest-log", json={"kind": kind, "text": "【Xcollector】...", "sent": True}, headers=H)
check("POST /digest-log 200", r.status_code == 200 and bool(r.json().get("id")), r.text[:160])
log_id = r.json()["id"]
r = c.post(f"{API}/digest-log", json={"kind": kind, "text": "重复提交", "sent": True}, headers=H)
check("重复提交同 (day,kind,sent) → 同 id", r.json().get("id") == log_id, r.text[:160])
r = c.post(f"{API}/digest-log", json={"kind": kind, "text": "失败的尝试", "sent": False}, headers=H)
check("sent 不同的记录是新行", r.json().get("id") != log_id, r.text[:160])
r = c.get(f"{API}/digest-log", params={"kind": kind, "sent": "true", "count_only": "1"}, headers=H)
check("digest-log count_only=1", set((r.json() or {}).keys()) == {"count"}, r.text[:160])
check("count_only 计数为 1（幂等）", r.json().get("count") == 1, r.text[:120])
r = c.get(f"{API}/digest-log", params={"kind": kind}, headers=H)
logs = r.json().get("logs", [])
check("列表按 ts 倒序、字段齐全", len(logs) == 2 and set(logs[0]) == {"id", "day", "kind", "text", "sent", "error", "ts"}, r.text[:240])
check("sent 以 bool 返回", logs[0].get("sent") is False and logs[1].get("sent") is True, str([x["sent"] for x in logs]))
check("day 省略 → 服务器当天", logs[0].get("day") == day, str(logs[0].get("day")))
r = c.get(f"{API}/digest-log", params={"day": day, "kind": kind, "sent": "false", "count_only": "1"}, headers=H)
check("day+kind+sent 过滤", r.json().get("count") == 1, r.text[:120])
check("sent 非布尔 → 400", c.get(f"{API}/digest-log", params={"sent": "maybe"}, headers=H).status_code == 400)

# ---------------------------------------------------------------------------
# 12. bot 键值暂存 + TTL（契约 §11）
# ---------------------------------------------------------------------------
print("\n--- bot_state ---")
ns = f"smoke_ns_{RUN}"
r = c.put(f"{API}/state/{ns}/draft", json={"value": {"text": "明天下午3点", "n": [1, 2, 3]}, "ttl_seconds": 1}, headers=H)
check("PUT /state 200", r.status_code == 200 and r.json().get("ok") is True, r.text[:160])
check("返回 expires_at", isinstance(r.json().get("expires_at"), int), r.text[:160])
r = c.get(f"{API}/state/{ns}/draft", headers=H)
check("GET /state 原样取回 JSON", r.json().get("value") == {"text": "明天下午3点", "n": [1, 2, 3]}, r.text[:200])
r = c.put(f"{API}/state/{ns}/draft", json={"value": "改小一点"}, headers=H)
check("重复 PUT 是 upsert（幂等）", r.status_code == 200 and r.json().get("expires_at") is None, r.text[:160])
check("upsert 覆盖旧值", c.get(f"{API}/state/{ns}/draft", headers=H).json().get("value") == "改小一点")
c.put(f"{API}/state/{ns}/forever", json={"value": 42}, headers=H)
c.put(f"{API}/state/{ns}/zero", json={"value": "立即过期", "ttl_seconds": 0}, headers=H)
r = c.get(f"{API}/state/{ns}/zero", headers=H)
check("ttl=0 → 立即 404", r.status_code == 404, f"HTTP {r.status_code}")
r = c.get(f"{API}/state/{ns}", headers=H)
keys = {i["key"] for i in r.json().get("items", [])}
check("namespace 列表含未过期项", keys == {"draft", "forever"}, str(keys))
check("列表项带 value/expires_at", all({"key", "value", "expires_at"} <= set(i) for i in r.json()["items"]), r.text[:200])
c.put(f"{API}/state/{ns}/ttl", json={"value": "1 秒后消失", "ttl_seconds": 1}, headers=H)
time.sleep(1.3)
check("TTL 到期后 GET → 404", c.get(f"{API}/state/{ns}/ttl", headers=H).status_code == 404)
r = c.get(f"{API}/state/{ns}", headers=H)
check("过期项不出现在 namespace 列表", {i["key"] for i in r.json()["items"]} == {"draft", "forever"}, r.text[:200])
r = c.get(f"{API}/state/{ns}", params={"count_only": "1"}, headers=H)
check("namespace count_only=1", (r.json() or {}).get("count") == 2, r.text[:120])
r = c.delete(f"{API}/state/{ns}/forever", headers=H)
check("DELETE → deleted=true", r.status_code == 200 and r.json().get("deleted") is True, r.text[:120])
check("删后 GET → 404", c.get(f"{API}/state/{ns}/forever", headers=H).status_code == 404)
check("重复 DELETE 也 200（幂等）", c.delete(f"{API}/state/{ns}/forever", headers=H).status_code == 200)
check("未知键 GET → 404", c.get(f"{API}/state/{ns}/nope", headers=H).status_code == 404)
check("ttl_seconds 为负 → 400", c.put(f"{API}/state/{ns}/bad", json={"value": 1, "ttl_seconds": -5}, headers=H).status_code == 400)
check("缺少 value → 422", c.put(f"{API}/state/{ns}/noval", json={"ttl_seconds": 5}, headers=H).status_code == 422)

# ---------------------------------------------------------------------------
print()
print(f"共 {total} 项断言")
if fails:
    print(f"❌ {len(fails)} 项失败：")
    for name in fails:
        print("   - " + name)
    sys.exit(1)
print("✅ 全部通过")
