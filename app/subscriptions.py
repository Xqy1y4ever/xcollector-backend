"""订阅：用户订的是「**谁**在**哪个群**说的话」。

## 为什么最小单位是 (群, 发送者)，而不是群

这条流水线里值得进清单的东西是**人**发的，不是群发的。如果允许"订这个群"，
就等于允许"这个群里任何人说话都进我的清单" —— 那正是整条工作流一开始要
避免的噪声，而且一旦有人这么订了，LLM 调用量和误报会一起失控。

所以这里**没有**"整个群"这个选项，而且是三层堵死：

  1. 表结构里 `sender_id` 是 NOT NULL；
  2. `normalize_sender()` 拒绝空值、拒绝 `*` / `all` / `全部` 这类通配符，
     也拒绝逗号分隔的多值（"一次订一批发送者"看起来像便利，实际上是把
     上面那条规则绕回去）；
  3. 群号和发送者都必须是 QQ 号形状 —— 参数写错时立刻报错，
     而不是安静地存下一条永远匹配不到任何消息的订阅。

第 3 条还有一层意思：订阅写错了**不会报错**，只会"什么都没有" ——
用户以为订上了，其实一直收不到。这是最难发现的一类故障，所以在入口就拦。

## 订阅 vs 白名单

订阅定义"**抽什么**"，bot 的群白名单定义"**看得到什么**"。两者都要满足：
订了一个白名单外的群不会报错，但也不会有东西进来。目录（`list_sources`）
只列得出 bot 实际处理过的来源，原因就在这里。
"""

from __future__ import annotations

from typing import Any

from .db import (
    count_subscriptions,
    delete_subscription,
    fetch_all,
    fetch_one,
    find_subscribers,
    get_subscription,
    insert_subscription,
    list_subscriptions,
    update_subscription,
)
from .users import UserError, normalize_qq

# 一个用户的订阅上限。防止误写的脚本把表灌爆，也顺手挡住"批量订阅"这种用法。
SUB_MAX_PER_USER = 200

# 明确表达"整个群"的词。单独挡在这里，是为了给出一条能看懂的报错，
# 而不是让它掉进"QQ 号格式不对"里 —— 两者对用户的意思完全不同。
_GROUP_WIDE = {
    "*",
    "all",
    "any",
    "全部",
    "所有",
    "所有人",
    "全群",
    "整个群",
    "群里所有人",
}

_NOTE_MAX = 200
_NAME_MAX = 80

# 目录一次最多返回多少条来源。前端是个下拉/列表，不需要无限长。
SOURCE_LIMIT = 200


class SubscriptionError(ValueError):
    """订阅参数不合法。routes.py 会把它翻成 400。"""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


def _clean_text(value: Any, *, field: str, limit: int) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if len(text) > limit:
        raise SubscriptionError(f"{field}最多 {limit} 个字")
    return text


def normalize_sender(raw: Any) -> str:
    """校验发送者。**"禁止整个群"就落在这个函数里。**"""
    text = str(raw or "").strip()
    if not text:
        raise SubscriptionError(
            "必须指定发送者 QQ 号：订阅的最小单位是「某个群里某个人说的话」，"
            "不支持订阅整个群"
        )
    if text.lower() in _GROUP_WIDE:
        raise SubscriptionError(
            f"不支持订阅整个群（sender_id={text!r}）：这样会把这个群里"
            "所有人的发言都算进来。请填发出通知的那个人的 QQ 号"
        )
    if "," in text or "，" in text:
        raise SubscriptionError("一次只能订一个发送者，请分开添加")
    try:
        return normalize_qq(text)
    except UserError as exc:
        raise SubscriptionError(f"发送者 QQ 号不像个 QQ 号：{text!r}（{exc}）") from exc


def normalize_group(raw: Any) -> str:
    text = str(raw or "").strip()
    if not text:
        raise SubscriptionError("必须指定群号")
    try:
        return normalize_qq(text)
    except UserError as exc:
        raise SubscriptionError(f"群号不像个 QQ 群号：{text!r}（{exc}）") from exc


def public_subscription(row: Any) -> dict:
    """对外的订阅形状。**不含 user_id** —— 调用方已经知道那是谁了。"""
    row = row or {}
    return {
        "id": str(row.get("id") or ""),
        "group_id": str(row.get("group_id") or ""),
        "sender_id": str(row.get("sender_id") or ""),
        "group_name": row.get("group_name"),
        "sender_name": row.get("sender_name"),
        "note": row.get("note"),
        "enabled": bool(row.get("enabled")),
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
    }


async def list_for_user(
    user_id: str, *, include_disabled: bool = True, limit: int | None = None
) -> list[dict]:
    rows = await list_subscriptions(user_id, include_disabled=include_disabled, limit=limit)
    return [public_subscription(row) for row in rows]


async def get(user_id: str, sub_id: str) -> dict | None:
    row = await get_subscription(user_id, sub_id)
    return public_subscription(row) if row else None


async def add(
    user_id: str,
    *,
    group_id: Any,
    sender_id: Any,
    group_name: Any = None,
    sender_name: Any = None,
    note: Any = None,
) -> tuple[dict, bool]:
    """新增或重新启用一条订阅。返回 `(订阅, 是否新建)`。

    上限只在**真的要新增**时检查：把一条已有的重新打开不该被上限挡住，
    否则"订满了"之后连自己原有的订阅都动不了。
    """
    group = normalize_group(group_id)
    sender = normalize_sender(sender_id)

    existing = await fetch_one(
        "SELECT id FROM subscription WHERE user_id=? AND group_id=? AND sender_id=?",
        (user_id, group, sender),
    )
    if existing is None and await count_subscriptions(user_id) >= SUB_MAX_PER_USER:
        raise SubscriptionError(f"订阅数已达上限 {SUB_MAX_PER_USER} 条，先删掉一些再加")

    row, created = await insert_subscription(
        user_id,
        group,
        sender,
        group_name=_clean_text(group_name, field="群名", limit=_NAME_MAX),
        sender_name=_clean_text(sender_name, field="发送者备注", limit=_NAME_MAX),
        note=_clean_text(note, field="备注", limit=_NOTE_MAX),
    )
    return public_subscription(row), created


async def patch(user_id: str, sub_id: str, payload: dict) -> dict | None:
    """改 enabled / note / 名字。只认白名单里的字段，其余一律忽略。

    返回 None = 这条订阅不存在（或不属于这个用户）—— 调用方翻成 404，
    不区分"不存在"和"是别人的"，免得 id 能被探测。
    """
    fields: dict[str, Any] = {}
    if payload.get("enabled") is not None:
        fields["enabled"] = 1 if payload["enabled"] else 0
    for key, label, limit in (
        ("note", "备注", _NOTE_MAX),
        ("group_name", "群名", _NAME_MAX),
        ("sender_name", "发送者备注", _NAME_MAX),
    ):
        if key in payload:
            fields[key] = _clean_text(payload[key], field=label, limit=limit)
    if not fields:
        raise SubscriptionError("没有要改的字段")
    row = await update_subscription(user_id, sub_id, fields)
    return public_subscription(row) if row else None


async def remove(user_id: str, sub_id: str) -> bool:
    return await delete_subscription(user_id, sub_id)


async def subscribers_for(group_id: str, sender_id: str | None = None) -> list[str]:
    """投递名单（bot 用，服务令牌专属）。

    落到 db 那一层的 `find_subscribers` —— 全项目唯一一处跨用户读，
    为什么它是安全的写在那个函数的 docstring 里。

    `sender_id=None` = "这个群里任何发送者"，只给缺口告警用（群级事件）。
    """
    return await find_subscribers(group_id, sender_id)


async def list_sources(
    *, limit: int = SOURCE_LIMIT, keyword: str | None = None
) -> list[dict]:
    """**信息源目录**：这套部署目前见过的 (群, 发送者) 组合。

    新用户注册后手上是空的 —— 没有通知、不知道群号，也就无从订阅。
    所以需要一份目录让他能挑，否则"用户自己配置订阅"这件事根本没法用。

    目录**从共享层 `raw_message` 聚合**，不碰任何用户表：
    它回答的是"这套部署看得见哪些来源"，而不是"谁收了多少"，
    所以不泄露任何按用户的数据（tests/check_subscriptions.py 有对应断言）。

    代价要说清楚：只有 **bot 实际处理过** 的组合才会出现在这里。
    """
    sql = (
        "SELECT group_id,"
        "       MAX(group_name) AS group_name,"
        "       sender_id,"
        "       MAX(sender_name) AS sender_name,"
        "       MAX(ts) AS last_ts,"
        "       COUNT(*) AS msg_count"
        " FROM raw_message"
        " WHERE group_id <> '' AND sender_id <> ''"
    )
    params: list[Any] = []
    if keyword and keyword.strip():
        like = f"%{keyword.strip()}%"
        sql += (
            " AND (group_name LIKE ? OR sender_name LIKE ?"
            " OR group_id LIKE ? OR sender_id LIKE ?)"
        )
        params.extend([like, like, like, like])
    sql += " GROUP BY group_id, sender_id ORDER BY last_ts DESC LIMIT ?"
    params.append(int(limit))

    rows = await fetch_all(sql, tuple(params))
    return [
        {
            "group_id": str(row.get("group_id") or ""),
            "group_name": row.get("group_name"),
            "sender_id": str(row.get("sender_id") or ""),
            "sender_name": row.get("sender_name"),
            "last_ts": row.get("last_ts"),
            "msg_count": int(row.get("msg_count") or 0),
        }
        for row in rows
    ]
