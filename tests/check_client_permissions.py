"""客户端（UserToken）的写入权限：能写什么、绝对不能写什么。

需要一个**正在运行的后端**：

    $env:API_TOKEN='service-token'; $env:SIGNUP_MODE='invite'; $env:SERVER_PORT='8005'
    .\\xcollector-backend\\.venv\\Scripts\\python.exe -m app.main

    $env:PERM_BASE='http://127.0.0.1:8005'; $env:PERM_SERVICE_TOKEN='service-token'
    .\\.venv\\Scripts\\python.exe -m tests.check_client_permissions

## 权限模型（2026-09 改过一版，这份文件跟着改了）

加了 `xcollector-client` 之后，入库方有两个，原文也有**两层**：

    服务令牌（bot）    → 共享层：raw_message / group_state
                         共享层是所有人订阅的群的并集，读它的人不止一个。
    用户令牌（客户端）  → 按用户的那一层：user_raw_message / 通知 / 统计 / 缺口 /
                         自己的键值 / 附件
                         **读**共享层永远只有服务令牌（跨群泄露）

关键变化：**客户端不再需要"证明自己订阅过某个来源"**。

老规则是"用户令牌也写共享层的 raw_message，但必须先证明订阅过这个来源"。它有两个
问题：一是订阅的单位是 (群, 发送者)、每人上限 200 条、不支持整群，而客户端手上是
一整个聊天记录库（几十万组合）—— 那条规则等于让客户端的功能不可用；二是它治不了
本、只是近似：共表时客户端可以抢先写一行 (群, 消息id)，bot 启动时的崩溃恢复
（`GET /api/messages?state=pending`，服务令牌、全站）会把这行捡走、抽取、扇出给别人。

现在归属写在**表**上：客户端写 `user_raw_message`，bot 写 `raw_message`。
越权在 SQL 层面就不可能发生，订阅也回到它本来的位置 —— 只影响 bot。

每一条"允许"都要配一条"拒绝"，否则权限放宽就等于没有边界。
"""

from __future__ import annotations

import os
import sys
import time

import httpx

BASE = os.environ.get("PERM_BASE", "http://127.0.0.1:8005").rstrip("/")
API = BASE + "/api"
SERVICE = os.environ.get("PERM_SERVICE_TOKEN", "service-token")

RUN = os.environ.get("PERM_RUN") or str(int(time.time() * 1000))
QQ_A = str(700000000 + int(RUN[-7:]) % 20000000)
QQ_B = str(int(QQ_A) + 1)
_SUF = RUN[-7:]
GROUP = "81" + _SUF            # A 订阅了的群
SENDER = "82" + _SUF
GROUP_OTHER = "83" + _SUF      # A 没订阅的群

H = {"Authorization": f"Bearer {SERVICE}"}
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
    c = httpx.Client(timeout=20)
    try:
        code = c.post(f"{API}/verify/request", json={"qq": qq}, headers=H).json()["code"]
        invite = c.post(
            f"{API}/invites", json={"note": f"perm-{RUN}", "max_uses": 1}, headers=H
        ).json()["code"]
        r = c.post(f"{API}/register", json={"qq": qq, "code": code, "invite_code": invite})
        assert r.status_code == 200, r.text
        body = r.json()
        return str(body["user"]["id"]), str(body["token"])
    finally:
        c.close()


def message_body(mid: str, *, group: str = GROUP, sender: str = SENDER, ts: int | None = None) -> dict:
    return {
        "message_id": mid,
        "group_id": group,
        "group_name": "权限测试群",
        "sender_id": sender,
        "sender_name": "张老师",
        "ts": ts or int(time.time() * 1000),
        "content": f"权限测试 {mid}",
        "attachments": [],
        "raw": {"test": True},
    }


def notification_body(raw_id: str, *, group: str = GROUP, sender: str = SENDER) -> dict:
    return {
        "raw_message_id": raw_id,
        "group_id": group,
        "group_name": "权限测试群",
        "sender_id": sender,
        "sender_name": "张老师",
        "source_ts": int(time.time() * 1000),
        "title": f"权限测试-{RUN}",
        "evidence": "证据",
    }


def raw_state_of(c: httpx.Client, token: str, nid: str):
    """通过"自己那条通知"读回原文的状态。

    用户令牌没有直接读原文的接口（那正是要守住的东西），而通知详情里带着原文 ——
    这两条路都只对通知的主人有效。
    """
    body = c.get(f"{API}/notifications/{nid}", headers=hdr(token)).json()
    return ((body or {}).get("raw") or {}).get("state")


def main() -> int:
    c = httpx.Client(timeout=20)
    print(f"→ {BASE}  RUN={RUN}\n")

    uid_a, tok_a = register(QQ_A)
    uid_b, tok_b = register(QQ_B)
    print(f"    A={uid_a}  B={uid_b}\n")

    # ------------------------------------------------------------------
    print("--- 1. 共享层的**读**：永远只有服务令牌 ---")
    # 这是最要紧的一条：共享层里是所有人订阅的所有群的消息，
    # 用户能读它就是跨群泄露。写能开、读不能开。
    check("用户令牌 GET /api/messages → 403", c.get(f"{API}/messages", headers=hdr(tok_a)).status_code, 403)
    check(
        "用户令牌 GET /api/messages/{id} → 403",
        c.get(f"{API}/messages/whatever", headers=hdr(tok_a)).status_code,
        403,
    )
    check("用户令牌 GET /api/groups → 403", c.get(f"{API}/groups", headers=hdr(tok_a)).status_code, 403)
    check("服务令牌 GET /api/messages → 200", c.get(f"{API}/messages", headers=H).status_code, 200)
    check("服务令牌 GET /api/groups → 200", c.get(f"{API}/groups", headers=H).status_code, 200)
    check("没令牌 GET /api/messages → 401", c.get(f"{API}/messages").status_code, 401)

    # ------------------------------------------------------------------
    print("\n--- 2. 客户端**一个订阅都没有**也能入库，但只写自己那一层 ---")
    # 这一节是这次改动的核心：客户端读的是自己账号的聊天记录，
    # "先订阅再上报"既表达不了它的输入，也表达不了它的权限。
    r = c.post(f"{API}/messages", json=message_body(f"perm-a-{RUN}"), headers=hdr(tok_a))
    check("没订阅任何来源 → POST /api/messages 仍然 200", r.status_code, 200)
    raw_a = str((r.json() or {}).get("id") or "")
    check_true("拿到 raw id", bool(raw_a), r.text[:160])
    check(
        "同一条再 POST 一次是幂等的（幂等键带 user_id）",
        c.post(f"{API}/messages", json=message_body(f"perm-a-{RUN}"), headers=hdr(tok_a))
        .json()
        .get("is_new"),
        False,
    )

    # 但它**不在**共享层里 —— 这是"客户端写不到共享层"最直接的证据
    check(
        "服务令牌按 id 读这条 → 404（它在 A 自己那层，不在共享层）",
        c.get(f"{API}/messages/{raw_a}", headers=H).status_code,
        404,
    )
    shared_ids = {
        m.get("id")
        for m in c.get(f"{API}/messages", params={"limit": 1000}, headers=H).json()["messages"]
    }
    check_true("共享层的列表里也没有它", raw_a not in shared_ids, raw_a)

    # 自己那一层：通过自己那条通知读得回来；别人读不到、也改不到
    nid_a = str(
        c.post(f"{API}/notifications", json=notification_body(raw_a), headers=hdr(tok_a)).json()["id"]
    )
    detail = c.get(f"{API}/notifications/{nid_a}", headers=hdr(tok_a)).json()
    check("自己的通知详情里读得回原文", ((detail or {}).get("raw") or {}).get("id"), raw_a)
    check(
        "B 读 A 的通知 → 404",
        c.get(f"{API}/notifications/{nid_a}", headers=hdr(tok_b)).status_code,
        404,
    )
    check(
        "A PATCH 自己的原文 → 200",
        c.patch(f"{API}/messages/{raw_a}", json={"state": "extracted"}, headers=hdr(tok_a)).status_code,
        200,
    )
    check(
        "B PATCH A 的原文 → 404（SQL 里就带 user_id）",
        c.patch(f"{API}/messages/{raw_a}", json={"state": "hacked"}, headers=hdr(tok_b)).status_code,
        404,
    )
    check("而 A 的原文没被 B 改动", raw_state_of(c, tok_a, nid_a), "extracted")

    # ------------------------------------------------------------------
    print("\n--- 3. 共享层（原文 / 群状态）：只有服务令牌能写 ---")
    check(
        "用户令牌 POST /api/groups → 403",
        c.post(
            f"{API}/groups",
            json={"group_id": GROUP, "last_msg_ts": int(time.time() * 1000)},
            headers=hdr(tok_a),
        ).status_code,
        403,
    )
    r = c.post(
        f"{API}/groups",
        json={"group_id": GROUP, "last_msg_ts": int(time.time() * 1000)},
        headers=hdr(tok_a),
    )
    check_true("报错说清了替代做法（本地镜像）", "镜像" in r.text, r.text[:160])

    r = c.post(f"{API}/messages", json=message_body(f"perm-svc-{RUN}"), headers=H)
    check("服务令牌写共享层 → 200", r.status_code, 200)
    raw_svc = str((r.json() or {}).get("id") or "")
    check("服务令牌读得到它", c.get(f"{API}/messages/{raw_svc}", headers=H).status_code, 200)
    check(
        "用户令牌 PATCH 共享层那一行 → 404（不是他的层）",
        c.patch(f"{API}/messages/{raw_svc}", json={"state": "hacked"}, headers=hdr(tok_a)).status_code,
        404,
    )

    # ------------------------------------------------------------------
    print("\n--- 4. 两个人在同一个群里各存一份，互不干扰 ---")
    # 同一条 (群, message_id)：两层都存得下，谁也不会把谁顶掉。
    # 老模型下这是做不到的（共享层是 UNIQUE(group_id, message_id)）。
    mid = f"perm-both-{RUN}"
    a_id = str(c.post(f"{API}/messages", json=message_body(mid), headers=hdr(tok_a)).json()["id"])
    b_id = str(c.post(f"{API}/messages", json=message_body(mid), headers=hdr(tok_b)).json()["id"])
    check_true("两个 raw id 不一样（各存各的）", a_id != b_id, f"{a_id} vs {b_id}")
    check(
        "A 再写同一条 → 幂等回到自己那个",
        c.post(f"{API}/messages", json=message_body(mid), headers=hdr(tok_a)).json().get("id"),
        a_id,
    )
    nid_a2 = str(
        c.post(f"{API}/notifications", json=notification_body(a_id), headers=hdr(tok_a)).json()["id"]
    )
    nid_b2 = str(
        c.post(f"{API}/notifications", json=notification_body(b_id), headers=hdr(tok_b)).json()["id"]
    )
    check(
        "A 改自己那条的 state → 200",
        c.patch(f"{API}/messages/{a_id}", json={"state": "mine"}, headers=hdr(tok_a)).status_code,
        200,
    )
    check("A 那条的 state 变了", raw_state_of(c, tok_a, nid_a2), "mine")
    check("B 那条的 state 没跟着变（各存各的）", raw_state_of(c, tok_b, nid_b2), "pending")
    check(
        "B 改 A 那条 → 404",
        c.patch(f"{API}/messages/{a_id}", json={"state": "hacked"}, headers=hdr(tok_b)).status_code,
        404,
    )

    # ------------------------------------------------------------------
    print("\n--- 5. 订阅还在，但只影响 bot ---")
    # 订了、退订了、订的是别的来源 —— 客户端照写自己那份。这正是"订阅只影响 bot"。
    for mid2, group, sender, label in (
        (f"perm-sub-{RUN}", GROUP, SENDER, "A 订阅的来源"),
        (f"perm-unsub-{RUN}", GROUP_OTHER, f"84{_SUF}", "A 没订阅的来源"),
    ):
        check(
            f"没订阅也照写：{label} → 200",
            c.post(
                f"{API}/messages", json=message_body(mid2, group=group, sender=sender), headers=hdr(tok_a)
            ).status_code,
            200,
        )

    r = c.post(
        f"{API}/subscriptions",
        json={"group_id": GROUP, "sender_id": SENDER, "group_name": "权限测试群"},
        headers=hdr(tok_a),
    )
    check("A 订一个来源 → 200", r.status_code, 200)
    subs = c.get(f"{API}/subscriptions", headers=hdr(tok_a)).json()["subscriptions"]
    c.delete(f"{API}/subscriptions/{subs[0]['id']}", headers=hdr(tok_a))
    check(
        "退订之后客户端照样写那个来源 → 200（订阅不管客户端）",
        c.post(f"{API}/messages", json=message_body(f"perm-after-unsub-{RUN}"), headers=hdr(tok_a)).status_code,
        200,
    )
    # 而订阅仍然是 bot 投递名单的来源
    check(
        "服务令牌读投递名单 → 200",
        c.get(
            f"{API}/subscriptions/routing",
            params={"group_id": GROUP, "sender_id": SENDER},
            headers=H,
        ).status_code,
        200,
    )

    # ------------------------------------------------------------------
    print("\n--- 6. 按用户的那一层：用户令牌可以直接写，但只会写到自己名下 ---")
    r = c.post(f"{API}/notifications", json=notification_body(raw_svc), headers=hdr(tok_a))
    check("用户令牌 POST /api/notifications → 200", r.status_code, 200)
    nid = str(r.json().get("id") or "")
    check_true("拿到通知 id", bool(nid), str(r.json()))

    # 归属**不能**从响应里读（响应只有 id/created），所以用"别人读不到"来证明 ——
    # 这比读一个字段更硬：它验的正是我们真正关心的那件事（隔离），
    # 而不是"某个字段等于某个值"。
    check("A 读得到 → 200", c.get(f"{API}/notifications/{nid}", headers=hdr(tok_a)).status_code, 200)
    check("B 读它 → 404（不在他名下）", c.get(f"{API}/notifications/{nid}", headers=hdr(tok_b)).status_code, 404)
    a_ids = {n.get("id") for n in c.get(f"{API}/notifications", headers=hdr(tok_a)).json()["notifications"]}
    b_ids = {n.get("id") for n in c.get(f"{API}/notifications", headers=hdr(tok_b)).json()["notifications"]}
    check_true("A 的列表里有它", nid in a_ids)
    check_true("B 的列表里没有它", nid not in b_ids)
    # 这条通知指向的是**共享层**的原文（服务令牌写的）：用户读自己通知里的原文
    # 走的是"先找自己那层，再找共享层"，两条都只对他自己那条通知有效。
    check(
        "自己的通知里也能读回共享层那条原文",
        ((c.get(f"{API}/notifications/{nid}", headers=hdr(tok_a)).json() or {}).get("raw") or {}).get("id"),
        raw_svc,
    )

    # 伪造 user_id 想写到别人名下：必须被无视。
    # 判据是"回到的还是同一条" —— 如果那个参数被当真了，它会新建成 B 的一条，
    # 拿到的就是另一个 id 了。
    r = c.post(
        f"{API}/notifications",
        params={"user_id": uid_b},
        json=notification_body(raw_svc),
        headers=hdr(tok_a),
    )
    check("用户令牌塞 ?user_id=B 之后拿到的还是同一条（参数被无视）", r.json().get("id"), nid)
    check("而 B 依然读不到它 → 404", c.get(f"{API}/notifications/{nid}", headers=hdr(tok_b)).status_code, 404)

    # 统计：只能记到自己名下
    r = c.post(f"{API}/stats", params={"user_id": uid_b}, json={"fields": {"extracted": 7}}, headers=hdr(tok_a))
    check("用户令牌 POST /api/stats(塞了别人的 user_id) → 200", r.status_code, 200)
    a_stat = c.get(f"{API}/stats", headers=hdr(tok_a)).json()
    b_stat = c.get(f"{API}/stats", headers=hdr(tok_b)).json()
    check("统计记到了 A 自己名下", int(a_stat.get("extracted") or 0), 7)
    check("B 的统计一点没动", int(b_stat.get("extracted") or 0), 0)

    # 缺口告警：同上，按用户
    r = c.post(
        f"{API}/gap-alerts",
        params={"user_id": uid_b},
        json={"group_id": GROUP, "from_ts": 1, "to_ts": 2, "reason": "测试"},
        headers=hdr(tok_a),
    )
    check("用户令牌 POST /api/gap-alerts → 200", r.status_code, 200)
    alerts_b = c.get(f"{API}/gap-alerts", headers=hdr(tok_b)).json()["alerts"]
    check("B 看不到 A 的缺口告警", alerts_b, [])

    # 键值暂存：客户端的游标就存这儿，必须能读能写
    r = c.put(
        f"{API}/state/client_cursor/nonexistent",
        json={"value": {"last": 123}},
        headers=hdr(tok_a),
    )
    check("用户令牌 PUT /api/state → 200", r.status_code, 200)
    r = c.get(f"{API}/state/client_cursor/nonexistent", headers=hdr(tok_a))
    check("读回自己写的游标", ((r.json() or {}).get("value") or {}).get("last"), 123)
    r = c.get(f"{API}/state/client_cursor/nonexistent", headers=hdr(tok_b))
    check("B 读同一个 key → 404（按用户隔离）", r.status_code, 404)
    check(
        "用户令牌 DELETE /api/state → 200",
        c.delete(f"{API}/state/client_cursor/nonexistent", headers=hdr(tok_a)).status_code,
        200,
    )

    # ------------------------------------------------------------------
    print("\n--- 7. 附件：能上传（没有归属可以检查，代价写在路由里）---")
    png = bytes.fromhex(
        "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
        "890000000a49444154789c63000100000500010d0a2db40000000049454e44ae426082"
    )
    r = c.post(
        f"{API}/attachments",
        headers=hdr(tok_a),
        files={"file": ("a.png", png, "image/png")},
        data={"filename": "a.png"},
    )
    check("用户令牌能上传附件 → 200", r.status_code, 200)
    att_id = str((r.json() or {}).get("id") or "")
    check_true("返回裸路径而不是签名", "sig=" not in str((r.json() or {}).get("url")), str(r.json()))

    # 签名 URL 仍然只对它签给的那个人有效
    r = c.patch(
        f"{API}/messages/{raw_svc}",
        json={"attachments": [{"id": att_id, "url": f"/api/attachments/{att_id}", "type": "image"}]},
        headers=H,
    )
    check("挂到原文上 → 200", r.status_code, 200)
    r = c.post(f"{API}/notifications", json=notification_body(raw_svc), headers=hdr(tok_a))
    nid = str(r.json().get("id") or "")
    signed = (
        c.get(f"{API}/notifications/{nid}", headers=hdr(tok_a)).json()["notification"]["attachments"][0]["url"]
    )
    check_true("读投影里是绑了 A 的签名 URL", "u=" in signed, signed)
    bare = httpx.Client(timeout=20)
    check("不带任何头取 A 的签名 URL → 200", bare.get(BASE + signed).status_code, 200)
    check(
        "把 u 改成 B → 401",
        bare.get(BASE + signed.replace(f"u={uid_a}", f"u={uid_b}")).status_code,
        401,
    )

    # ------------------------------------------------------------------
    print("\n--- 8. 仍然只有服务令牌能做的事 ---")
    check(
        "用户令牌签发验证码 → 403",
        c.post(f"{API}/verify/request", json={"qq": QQ_A}, headers=hdr(tok_a)).status_code,
        403,
    )
    check("用户令牌发邀请码 → 403", c.post(f"{API}/invites", json={}, headers=hdr(tok_a)).status_code, 403)
    check("用户令牌列用户 → 403", c.get(f"{API}/users", headers=hdr(tok_a)).status_code, 403)
    check(
        "用户令牌按 QQ 查用户 → 403",
        c.get(f"{API}/users/lookup", params={"qq": QQ_B}, headers=hdr(tok_a)).status_code,
        403,
    )
    # 投递名单是**全局**的：用户能读就等于看到了别人的订阅关系
    check(
        "用户令牌读投递名单 → 403",
        c.get(
            f"{API}/subscriptions/routing",
            params={"group_id": GROUP, "sender_id": SENDER},
            headers=hdr(tok_a),
        ).status_code,
        403,
    )
    # 改机器字段 / 删通知：那是"重跑抽取"和运维的动作，不是用户的自助操作
    check(
        "用户令牌 PATCH 通知的机器字段 → 403",
        c.patch(f"{API}/notifications/{nid}", json={"title": "改标题"}, headers=hdr(tok_a)).status_code,
        403,
    )
    check(
        "用户令牌 DELETE 通知 → 403",
        c.delete(f"{API}/notifications/{nid}", headers=hdr(tok_a)).status_code,
        403,
    )
    check(
        "用户令牌写 digest-log → 403（那是 bot 的动作）",
        c.post(f"{API}/digest-log", json={"kind": "auto", "text": "x", "sent": True}, headers=hdr(tok_a)).status_code,
        403,
    )

    c.close()
    print()
    if fails:
        print(f"❌ {len(fails)}/{total} 条失败：")
        for name in fails:
            print(f"   - {name}")
        return 1
    print(f"✅ {total} 条断言全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
