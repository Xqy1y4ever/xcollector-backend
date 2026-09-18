"""把库里的行变成 API 视图（读投影）。

核心逻辑只有一条：**correction 表覆盖 notification 表**，再由 `due_at` 推导
`status`。这样重跑（改 prompt、换模型）永远不会覆盖人工修正过的字段。

契约把这件事特许留在后端：前端直接消费后端，必须拿到"人工修正已生效、
status 已推导"的视图。这是「查」，不是业务判断。

为什么整段投影是 SQL：列表接口要一次拿回 N 行，如果在 Python 里逐行再查
corrections / read_state 就是 N+1。这里的做法是**一次 JOIN + 相关子查询**
（子查询走 `correction(notification_id, field, ts)` 索引），一次查询出全部行；
`status` 过滤、`q` 搜索、`since` 增量也都在同一层 SQL 里做，
于是 `limit` 的语义才是对的（先筛再截断，而不是截断了再在内存里筛）。
"""

from __future__ import annotations

from typing import Any, Sequence

from .db import count_of, fetch_all, fetch_one
from .signing import sign_attachments
from .utils import json_loads, now_ms

# 只追加的人工修正层里，允许出现的字段（契约 §2）
CORRECTABLE_FIELDS = ("title", "summary", "location", "due_at", "due_text", "status")


def _latest_id(field: str) -> str:
    """该通知该字段是否存在人工修正（NULL 既可能是"没改过"也可能是"改成 NULL"，
    所以要单独取一次 id 来区分）。

    子查询里 `c.user_id = n.user_id` **不能省**：correction 是用户表，
    少这个条件就可能把别人的修正读进来 —— 而新通知恰好没有修正时，
    这个 bug 是看不出来的。
    """
    return (
        "(SELECT c.id FROM correction c WHERE c.notification_id = n.id"
        " AND c.user_id = n.user_id"
        f" AND c.field = '{field}' ORDER BY c.ts DESC, c.id DESC LIMIT 1)"
    )


def _latest_value(field: str) -> str:
    return (
        "(SELECT c.value FROM correction c WHERE c.notification_id = n.id"
        " AND c.user_id = n.user_id"
        f" AND c.field = '{field}' ORDER BY c.ts DESC, c.id DESC LIMIT 1)"
    )


def _due_at_sql() -> str:
    return (
        f"CASE WHEN {_latest_id('due_at')} IS NOT NULL"
        f" THEN CAST({_latest_value('due_at')} AS INTEGER) ELSE n.due_at END"
    )


def _status_sql(now: int) -> str:
    """人工 status 优先；否则 due_at 已过 → expired，其余 active。"""
    due = _due_at_sql()
    return (
        f"CASE WHEN {_latest_id('status')} IS NOT NULL"
        f" AND COALESCE({_latest_value('status')}, '') <> ''"
        f" THEN {_latest_value('status')}"
        f" WHEN {due} IS NOT NULL AND {due} < {int(now)} THEN 'expired'"
        " ELSE 'active' END"
    )


def _projection_sql(now: int | None = None) -> str:
    moment = now_ms() if now is None else now
    return f"""
WITH proj AS (
  SELECT
    n.id                                          AS id,
    n.user_id                                     AS user_id,
    n.group_id                                    AS group_id,
    n.group_name                                  AS group_name,
    n.sender_id                                   AS sender_id,
    n.sender_name                                 AS sender_name,
    CASE WHEN {_latest_id('title')} IS NOT NULL
              AND COALESCE({_latest_value('title')}, '') <> ''
         THEN {_latest_value('title')} ELSE n.title END            AS title,
    CASE WHEN {_latest_id('summary')} IS NOT NULL
         THEN {_latest_value('summary')} ELSE n.summary END        AS summary,
    CASE WHEN {_latest_id('location')} IS NOT NULL
         THEN NULLIF({_latest_value('location')}, '') ELSE n.location END AS location,
    {_due_at_sql()}                                                AS due_at,
    CASE WHEN {_latest_id('due_text')} IS NOT NULL
         THEN {_latest_value('due_text')} ELSE n.due_text END      AS due_text,
    n.due_confidence                              AS due_confidence,
    n.conflict                                    AS conflict,
    n.candidates                                  AS candidates,
    n.evidence                                    AS evidence,
    {_status_sql(moment)}                         AS status,
    (SELECT COUNT(*) FROM correction c WHERE c.notification_id = n.id
                                             AND c.user_id = n.user_id)
                                                  AS correction_count,
    (SELECT rs.read_at FROM read_state rs WHERE rs.notification_id = n.id
                                             AND rs.user_id = n.user_id)
                                                  AS read_at,
    n.extractor                                   AS extractor,
    n.model                                       AS model,
    n.prompt_ver                                  AS prompt_ver,
    n.source_ts                                   AS source_ts,
    -- raw_message_id 也透出来：入库客户端要靠它建"这条源消息我处理过"的集合。
    -- 游标丢了之后，这个集合能让客户端**跳过重新抽取**（也就是不重复花模型的
    -- 钱）；而直接读原文的接口是服务令牌专属的，用户令牌下没有别的办法拿到 raw id。
    -- 它是这条通知自己的字段，不泄露任何别人的东西。
    n.raw_message_id                              AS raw_message_id,
    n.created_at                                  AS created_at,
    n.updated_at                                  AS updated_at,
    -- 原文可能在两层里的任何一层：客户端写的在**他自己**那张表里，bot 写的在
    -- 共享层。两个 LEFT JOIN + COALESCE，比"先查一层再查另一层"少一次往返，
    -- 也不会因为漏了一个分支而让附件静默消失（附件丢了界面上只是"没有图"）。
    COALESCE(ur.attachments, r.attachments)        AS raw_attachments
  FROM notification n
  LEFT JOIN user_raw_message ur
         ON ur.id = n.raw_message_id AND ur.user_id = n.user_id
  LEFT JOIN raw_message r ON r.id = n.raw_message_id
)
"""


_LIKE_ESCAPE = "\\"


def like_pattern(needle: str) -> str:
    escaped = (
        needle.replace(_LIKE_ESCAPE, _LIKE_ESCAPE * 2)
        .replace("%", _LIKE_ESCAPE + "%")
        .replace("_", _LIKE_ESCAPE + "_")
    )
    return f"%{escaped}%"


def _notification_where(
    user_id: str,
    *,
    notif_id: str | None = None,
    since: int | None = None,
    status: str | None = None,
    q: str | None = None,
) -> tuple[str, list[Any]]:
    # user_id 是**必需**条件，放最前面，且不参与"要不要加 WHERE"的判断
    clauses: list[str] = ["user_id = ?"]
    params: list[Any] = [user_id]
    if notif_id is not None:
        clauses.append("id = ?")
        params.append(notif_id)
    if since is not None:
        clauses.append("updated_at > ?")
        params.append(int(since))
    if status and status != "all":
        clauses.append("status = ?")
        params.append(status)
    if q:
        pattern = like_pattern(q.strip())
        clauses.append(
            f"(title LIKE ? ESCAPE '{_LIKE_ESCAPE}'"
            f" OR summary LIKE ? ESCAPE '{_LIKE_ESCAPE}'"
            f" OR evidence LIKE ? ESCAPE '{_LIKE_ESCAPE}')"
        )
        params.extend([pattern, pattern, pattern])
    return " WHERE " + " AND ".join(clauses), params


async def list_notification_views(
    user_id: str,
    *,
    since: int | None = None,
    status: str = "all",
    q: str | None = None,
    limit: int = 500,
) -> list[dict]:
    where, params = _notification_where(user_id, since=since, status=status, q=q)
    sql = (
        _projection_sql()
        + f"SELECT * FROM proj{where}"
        + " ORDER BY due_at IS NULL, due_at ASC, source_ts DESC LIMIT ?"
    )
    rows = await fetch_all(sql, (*params, int(limit)))
    return [to_view(row) for row in rows]


async def count_notification_views(
    user_id: str,
    *,
    since: int | None = None,
    status: str = "all",
    q: str | None = None,
) -> int:
    where, params = _notification_where(user_id, since=since, status=status, q=q)
    return await count_of(_projection_sql() + f"SELECT COUNT(*) AS c FROM proj{where}", params)


async def get_notification_view(user_id: str, notif_id: str) -> dict | None:
    where, params = _notification_where(user_id, notif_id=notif_id)
    row = await fetch_one(_projection_sql() + f"SELECT * FROM proj{where}", params)
    return to_view(row) if row else None


def to_view(row: dict) -> dict:
    """读投影对象的字段顺序与契约 §2 的表格一一对应。"""
    return {
        "id": row["id"],
        "group_id": row.get("group_id"),
        "group_name": row.get("group_name"),
        "sender_id": row.get("sender_id"),
        "sender_name": row.get("sender_name"),
        "title": row.get("title"),
        "summary": row.get("summary"),
        "location": row.get("location"),
        "due_at": row.get("due_at"),
        "due_text": row.get("due_text"),
        "due_confidence": float(row.get("due_confidence") or 0.0),
        "conflict": bool(row.get("conflict")),
        "candidates": json_loads(row.get("candidates"), []),
        "evidence": row.get("evidence") or "",
        "status": row.get("status"),
        "manually_edited": bool(row.get("correction_count")),
        "read": row.get("read_at") is not None,
        "attachments": sign_attachments(str(row["user_id"]), json_loads(row.get("raw_attachments"), [])),
        "extractor": row.get("extractor"),
        "model": row.get("model"),
        "prompt_ver": row.get("prompt_ver"),
        "source_ts": row.get("source_ts"),
        # 入库客户端用它建"我处理过哪些源消息"的集合（见上面 SELECT 里的说明）
        "raw_message_id": row.get("raw_message_id"),
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
    }


# 读投影对象的全部字段名（自检脚本用它断言"字段一个都不少"）
VIEW_FIELDS = (
    "id",
    "group_id",
    "group_name",
    "sender_id",
    "sender_name",
    "title",
    "summary",
    "location",
    "due_at",
    "due_text",
    "due_confidence",
    "conflict",
    "candidates",
    "evidence",
    "status",
    "manually_edited",
    "read",
    "attachments",
    "extractor",
    "model",
    "prompt_ver",
    "source_ts",
    "raw_message_id",
    "created_at",
    "updated_at",
)


def raw_view(row: dict | None, user_id: str = "") -> dict | None:
    """原始层一行（`raw_message` 或 `user_raw_message`）的对外形状。

    含 content / attachments / raw / state。`user_id` 只用来签附件链接 ——
    原文可能是**共享层**的（bot 写的，属于所有订阅了这条消息的人），也可能是
    **这个用户自己那份**（他的客户端写的），但附件链接必须只对请求者有效。

    两层共用一个形状是刻意的：调用方只拿得到"自己那条通知指向的那一行"，
    不需要、也不该知道它来自哪张表。
    """
    if row is None:
        return None
    return {
        "id": row.get("id"),
        "message_id": row.get("message_id"),
        "group_id": row.get("group_id"),
        "group_name": row.get("group_name"),
        "sender_id": row.get("sender_id"),
        "sender_name": row.get("sender_name"),
        "ts": row.get("ts"),
        "content": row.get("content") or "",
        "attachments": sign_attachments(user_id, json_loads(row.get("attachments"), [])),
        "raw": json_loads(row.get("raw"), {}),
        "ingested_at": row.get("ingested_at"),
        "state": row.get("state"),
        "state_reason": row.get("state_reason"),
        "state_at": row.get("state_at"),
    }


def raw_views(rows: Sequence[dict], user_id: str = "") -> list[dict]:
    return [raw_view(row, user_id) for row in rows]
