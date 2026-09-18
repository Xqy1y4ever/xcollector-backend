"""客户端（UserToken）的写入权限：能写什么、绝对不能写什么。

需要一个**正在运行的后端**：

    $env:API_TOKEN='service-token'; $env:SIGNUP_MODE='invite'; $env:SERVER_PORT='8005'
    .\\xcollector-backend\\.venv\\Scripts\\python.exe -m app.main

    $env:PERM_BASE='http://127.0.0.1:8005'; $env:PERM_SERVICE_TOKEN='service-token'
    .\\.venv\\Scripts\\python.exe -m tests.check_client_permissions

## 为什么单独一个文件

加了 `xcollector-client` 之后，**用户令牌第一次能写共享层**（`raw_message` /
`group_state`），而那是所有人看到的那张表。权限模型在这里从"两种令牌两种等级"
变成了有边界的规则：

    写按用户的那一层（通知/统计/缺口/自己的键值）→ 归属强制成他自己，直接允许
    写共享层（原文/群状态）                      → 必须先证明"这个来源我订阅了"
    **读**共享层                                → 永远只有服务令牌

最后一条是不对称的、也是有意的：共享层里是所有人订阅的所有群的消息，
让任何一个用户读到就是跨群泄露。写能开、读不能开，这是这份文件最该守住的东西。

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
    print("\n--- 2. 还没订阅 → 不能往共享层写 ---")
    r = c.post(f"{API}/messages", json=message_body(f"perm-a-{RUN}"), headers=hdr(tok_a))
    check("用户令牌没订阅就 POST /api/messages → 403", r.status_code, 403)
    check_true("报错说清了是因为没订阅", "订阅" in r.text, r.text[:160])
    check(
        "用户令牌没订阅就 POST /api/groups → 403",
        c.post(
            f"{API}/groups",
            json={"group_id": GROUP, "last_msg_ts": int(time.time() * 1000)},
            headers=hdr(tok_a),
        ).status_code,
        403,
    )

    # ------------------------------------------------------------------
    print("\n--- 3. 订了之后：能写自己订阅的来源 ---")
    r = c.post(
        f"{API}/subscriptions",
        json={"group_id": GROUP, "sender_id": SENDER, "group_name": "权限测试群"},
        headers=hdr(tok_a),
    )
    check("A 用用户令牌订一个来源 → 200", r.status_code, 200)

    r = c.post(f"{API}/messages", json=message_body(f"perm-a-{RUN}"), headers=hdr(tok_a))
    check("订了之后 POST /api/messages → 200", r.status_code, 200)
    raw_a = str((r.json() or {}).get("id") or "")
    check_true("拿到 raw id", bool(raw_a), str(r.json()))
    check("同一条再 POST 一次是幂等的", c.post(
        f"{API}/messages", json=message_body(f"perm-a-{RUN}"), headers=hdr(tok_a)
    ).json().get("is_new"), False)

    # 群状态：订了这个群里**任何一个**发送者就够
    r = c.post(
        f"{API}/groups",
        json={"group_id": GROUP, "group_name": "权限测试群", "last_msg_ts": int(time.time() * 1000)},
        headers=hdr(tok_a),
    )
    check("订了之后 POST /api/groups → 200", r.status_code, 200)

    # PATCH 原文：门槛按**那一行自己的来源**判，而不是请求体（请求体里没有这些字段）
    r = c.patch(f"{API}/messages/{raw_a}", json={"state": "extracted"}, headers=hdr(tok_a))
    check("订了之后 PATCH 自己来源的原文 → 200", r.status_code, 200)

    # ------------------------------------------------------------------
    print("\n--- 4. 但只限自己订阅的来源 ---")
    r = c.post(
        f"{API}/messages",
        json=message_body(f"perm-other-{RUN}", group=GROUP_OTHER, sender=f"84{_SUF}"),
        headers=hdr(tok_a),
    )
    check("写到没订阅的来源 → 403", r.status_code, 403)
    r = c.post(
        f"{API}/messages",
        json=message_body(f"perm-othersender-{RUN}", group=GROUP, sender=f"85{_SUF}"),
        headers=hdr(tok_a),
    )
    check("同一个群里**别的发送者**也 → 403（订阅的最小单位是人）", r.status_code, 403)
    check(
        "没订阅的群 POST /api/groups → 403",
        c.post(
            f"{API}/groups",
            json={"group_id": GROUP_OTHER, "last_msg_ts": int(time.time() * 1000)},
            headers=hdr(tok_a),
        ).status_code,
        403,
    )

    # B 没订阅任何东西，所以它连 A 订阅了的那个来源也写不了
    r = c.post(f"{API}/messages", json=message_body(f"perm-b-{RUN}"), headers=hdr(tok_b))
    check("B 没订阅，写 A 订的来源 → 403（订阅是按用户的）", r.status_code, 403)

    # 退订之后连自己原来的来源也不能写了
    subs = c.get(f"{API}/subscriptions", headers=hdr(tok_a)).json()["subscriptions"]
    c.delete(f"{API}/subscriptions/{subs[0]['id']}", headers=hdr(tok_a))
    r = c.post(
        f"{API}/messages",
        json=message_body(f"perm-after-unsub-{RUN}"),
        headers=hdr(tok_a),
    )
    check("退订之后就不能再写那个来源了 → 403", r.status_code, 403)

    # 服务令牌不受影响：它是整套部署的入库方，不需要订阅
    r = c.post(f"{API}/messages", json=message_body(f"perm-svc-{RUN}"), headers=H)
    check("服务令牌写共享层不用订阅 → 200", r.status_code, 200)

    # ------------------------------------------------------------------
    print("\n--- 5. 按用户的那一层：用户令牌可以直接写，但只会写到自己名下 ---")
    # 服务令牌造一条原文（用户令牌已经退订了，写不了）
    raw_svc = str(c.post(f"{API}/messages", json=message_body(f"perm-svc2-{RUN}"), headers=H).json()["id"])

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
    print("\n--- 6. 附件：能上传（没有归属可以检查，代价写在路由里）---")
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
    print("\n--- 7. 仍然只有服务令牌能做的事 ---")
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
