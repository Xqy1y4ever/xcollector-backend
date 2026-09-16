"""HTTP 接口端到端冒烟测试。

需要一个**正在运行的后端**，并且必须用 `EXTRACTOR=rule` 启动，
否则手动建任务那条会真的去调大模型、消耗你的 API key。

    # 终端 1
    $env:EXTRACTOR='rule'; python -m app.main

    # 终端 2
    $env:SMOKE_BASE='http://127.0.0.1:8000'; python -m tests.check_api

覆盖：bot 推消息入口（含白名单过滤与去重）、手动建任务的两条分支
（有把握直接建 / 没把握回问 / force_commit 强建）、location 与 status=done
的修正、以及 bot 不可达时 health 要如实报告而不是报错。

脚本会往你的库里写数据（message_id 以 `smoke-` 开头），重复运行是安全的：
主键约束会识别出重复推送，测试本身也断言了这一点。
"""

import json
import os
import sys

import httpx

B = os.environ.get("SMOKE_BASE", "http://127.0.0.1:8000") + "/api"
GROUP = "673504310"
c = httpx.Client(timeout=20)
fails = []


def check(name, cond, detail=""):
    print(("ok    " if cond else "FAIL  ") + name + (f"  {detail}" if detail else ""))
    if not cond:
        fails.append(name)


def msg(mid, group, text, sender="10001", name="李老师"):
    return {
        "source": "qq",
        "message_id": mid,
        "group_id": group,
        "group_name": "NOVA官方通知群",
        "sender_id": sender,
        "sender_name": name,
        "ts": 1789552749000,
        "text": text,
        "at_all": False,
        "mentions": [],
        "reply_to": None,
        "attachments": [],
        "raw": {"smoke": True},
    }


# 1. health 应当显示 bot 不可达而不是报错
r = c.get(f"{B}/health")
h = r.json()
check("GET /health 200", r.status_code == 200)
bot = h.get("bot") or {}
check("bot.reachable == False（bot 没起）", bot.get("reachable") is False, f"last_error={str(bot.get('last_error'))[:60]}")
check("health 顶层仍有 onebot 键（前端兼容）", "onebot" in h)

# 2. ingest：一个白名单群 + 一个非白名单群
body = {"messages": [
    msg("smoke-01", GROUP, "冒烟测试：本周日10:00在体育馆集合，请全体同学参加。"),
    msg("smoke-02", "999999999", "别的群的消息"),
]}
r = c.post(f"{B}/ingest/messages", json=body)
d = r.json()
check("POST /ingest/messages 200", r.status_code == 200, json.dumps(d, ensure_ascii=False))
check("accepted=1", d.get("accepted") == 1)
check("filtered=1（群白名单生效）", d.get("filtered") == 1)

# 3. 重复推送
r = c.post(f"{B}/ingest/messages", json=body)
d = r.json()
check("重复推送被识别", d.get("duplicates") == 1, json.dumps(d, ensure_ascii=False))

# 4. 列表里能看到，且带 location
r = c.get(f"{B}/notifications", params={"q": "冒烟测试"})
lst = r.json()["notifications"]
check("列表能查到刚推的通知", len(lst) == 1, f"命中 {len(lst)} 条")
if lst:
    n = lst[0]
    check("location 抽取正确", n.get("location") == "体育馆", f"location={n.get('location')!r}")
    check("来源是群名", n.get("group_name") == "NOVA官方通知群", f"group_name={n.get('group_name')!r}")
    nid = n["id"]
else:
    nid = None

# 5. 手动建任务：有把握 → 直接建
r = c.post(f"{B}/tasks/manual", json={
    "text": "明天下午3点在教三201交实验报告",
    "sender_id": "242684313", "sender_name": "我", "auto_commit": True,
})
d = r.json()
check("POST /tasks/manual 200", r.status_code == 200, json.dumps(d, ensure_ascii=False)[:200])
check("有把握时不回问", d.get("needs_confirm") is False, f"reason={d.get('reason')}")
check("手动任务已建", bool(d.get("task")))
check("手动任务抓到地点", (d.get("preview") or {}).get("location") == "教三201", f"{(d.get('preview') or {}).get('location')!r}")

# 6. 手动建任务：没时间 → 回问，不建
r = c.post(f"{B}/tasks/manual", json={"text": "随便写点什么", "auto_commit": True})
d = r.json()
check("没时间时回问", d.get("needs_confirm") is True, f"reason={d.get('reason')}")
check("回问时不建条", d.get("task") is None)

# 7. force_commit 强制建
r = c.post(f"{B}/tasks/manual", json={"text": "随便写点什么", "auto_commit": True, "force_commit": True})
d = r.json()
check("force_commit 强制建条", bool(d.get("task")))

# 8. 修正 location
if nid:
    r = c.post(f"{B}/notifications/{nid}/corrections", json={"field": "location", "value": "教三301", "user_id": "smoke"})
    d = r.json()
    check("修正 location 200", r.status_code == 200)
    check("location 修正生效", d["notification"].get("location") == "教三301")

    # 9. 标记完成
    r = c.post(f"{B}/notifications/{nid}/corrections", json={"field": "status", "value": "done", "user_id": "smoke"})
    d = r.json()
    check("status=done 被接受", r.status_code == 200, r.text[:150])
    check("status 生效", d["notification"].get("status") == "done")

    # 10. status=done 过滤
    r = c.get(f"{B}/notifications", params={"status": "done"})
    check("?status=done 能筛到", any(x["id"] == nid for x in r.json()["notifications"]))

    # 清理
    c.post(f"{B}/notifications/{nid}/corrections", json={"field": "status", "value": "archived", "user_id": "smoke"})

print()
print(f"❌ {len(fails)} 项失败: {fails}" if fails else "✅ 全部通过")
sys.exit(1 if fails else 0)
