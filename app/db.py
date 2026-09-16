"""SQLite 数据访问层。

分层铁律（见 docs/design.md）：
  - raw_message 是唯一不可再生的资产，**只追加**，永不修改内容
    （只有 state* 三个状态列会更新，用于标记"这条处理到哪一步了"）
  - notification 是派生层，可整表重建
  - correction 是人工修正，**只追加**。展示时用 correction 覆盖 notification
    里对应字段，这样重跑抽取永远不会覆盖人工修正。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, Iterable, Sequence

import aiosqlite

from .config import get_settings
from .utils import local_day, new_id, now_ms

logger = logging.getLogger(__name__)

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA busy_timeout=5000;
PRAGMA foreign_keys=ON;

-- ==================== 原始层：只追加 ====================
CREATE TABLE IF NOT EXISTS raw_message (
  id            TEXT PRIMARY KEY,          -- 内部 ID
  message_id    TEXT NOT NULL,             -- OneBot 的 message_id
  group_id      TEXT NOT NULL,
  group_name    TEXT,
  sender_id     TEXT NOT NULL,
  sender_name   TEXT,
  ts            INTEGER NOT NULL,          -- 消息发送时间（相对时间解析的唯一锚点）
  content       TEXT NOT NULL DEFAULT '',
  attachments   TEXT NOT NULL DEFAULT '[]',-- JSON: [{type,url,local_path,extracted_text}]
  raw           TEXT NOT NULL,             -- OneBot 原始事件 JSON，永不丢字段
  ingested_at   INTEGER NOT NULL,
  -- 处理状态：pending | extracted | noise | skipped_whitelist | unparsed | degraded
  -- noise = 判定为闲聊/回执，属于正常结果；unparsed/degraded 才是真正的盲区
  state         TEXT NOT NULL DEFAULT 'pending',
  state_reason  TEXT,
  state_at      INTEGER,
  UNIQUE(group_id, message_id)
);
CREATE INDEX IF NOT EXISTS ix_raw_ts       ON raw_message(ts DESC);
CREATE INDEX IF NOT EXISTS ix_raw_group_ts ON raw_message(group_id, ts DESC);
CREATE INDEX IF NOT EXISTS ix_raw_state    ON raw_message(state, ts DESC);

-- ==================== 派生层：可整表重建 ====================
CREATE TABLE IF NOT EXISTS notification (
  id             TEXT PRIMARY KEY,
  raw_message_id TEXT NOT NULL,
  group_id       TEXT NOT NULL,
  group_name     TEXT,
  sender_id      TEXT,
  sender_name    TEXT,
  source_ts      INTEGER NOT NULL,
  title          TEXT NOT NULL,
  summary        TEXT,
  location       TEXT,                     -- 通知里的地点，如「教三201」；NULL = 原文没提
  due_at         INTEGER,                  -- NULL = 没解析出确定时间（合法状态）
  due_text       TEXT,                     -- 原文时间表达，如「下周三前」
  due_confidence REAL NOT NULL DEFAULT 0,
  evidence       TEXT NOT NULL,            -- 支撑结论的原文片段，**必须非空**
  conflict       INTEGER NOT NULL DEFAULT 0,
  candidates     TEXT NOT NULL DEFAULT '[]',-- JSON: 各模型给出的 due_at
  extractor      TEXT,                     -- llm | rule
  model          TEXT,
  prompt_ver     TEXT,
  created_at     INTEGER NOT NULL,
  updated_at     INTEGER NOT NULL,
  UNIQUE(raw_message_id)
);
CREATE INDEX IF NOT EXISTS ix_notif_due     ON notification(due_at);
CREATE INDEX IF NOT EXISTS ix_notif_updated ON notification(updated_at DESC);
CREATE INDEX IF NOT EXISTS ix_notif_group   ON notification(group_id, source_ts DESC);

-- ==================== 人工修正：只追加 ====================
CREATE TABLE IF NOT EXISTS correction (
  id              TEXT PRIMARY KEY,
  notification_id TEXT NOT NULL,
  field           TEXT NOT NULL,           -- title|summary|location|due_at|due_text|status
  value           TEXT,                    -- 统一存字符串，读取时按字段类型还原
  user_id         TEXT,
  ts              INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_corr_notif ON correction(notification_id, field, ts);

-- ==================== 已读状态 ====================
CREATE TABLE IF NOT EXISTS read_state (
  notification_id TEXT PRIMARY KEY,
  read_at         INTEGER NOT NULL
);

-- ==================== 群状态（用于缺口检测）====================
CREATE TABLE IF NOT EXISTS group_state (
  group_id        TEXT PRIMARY KEY,
  group_name      TEXT,
  last_msg_ts     INTEGER,
  last_msg_at     INTEGER,
  msg_count_today INTEGER NOT NULL DEFAULT 0,
  count_date      TEXT
);

-- ==================== 缺口告警 ====================
CREATE TABLE IF NOT EXISTS gap_alert (
  id           TEXT PRIMARY KEY,
  group_id     TEXT NOT NULL,
  group_name   TEXT,
  from_ts      INTEGER NOT NULL,
  to_ts        INTEGER NOT NULL,
  reason       TEXT,
  created_at   INTEGER NOT NULL,
  acknowledged INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_gap_created ON gap_alert(created_at DESC);

-- ==================== 流水线统计 ====================
CREATE TABLE IF NOT EXISTS pipeline_stat (
  day           TEXT PRIMARY KEY,
  ingested      INTEGER NOT NULL DEFAULT 0,
  extracted     INTEGER NOT NULL DEFAULT 0,
  unparsed      INTEGER NOT NULL DEFAULT 0,
  conflicts     INTEGER NOT NULL DEFAULT 0,
  degraded      INTEGER NOT NULL DEFAULT 0,
  llm_tokens    INTEGER NOT NULL DEFAULT 0
);

-- ==================== digest 发送记录 ====================
CREATE TABLE IF NOT EXISTS digest_log (
  id      TEXT PRIMARY KEY,
  day     TEXT NOT NULL,
  kind    TEXT NOT NULL,                   -- auto | manual | preview
  text    TEXT NOT NULL,
  sent    INTEGER NOT NULL DEFAULT 0,
  error   TEXT,
  ts      INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_digest_day ON digest_log(day, kind);
"""

_conn: aiosqlite.Connection | None = None
_write_lock = asyncio.Lock()

# 增量迁移：(表, 列, 列定义)
# SQLite 没有 ADD COLUMN IF NOT EXISTS，只能先读表结构再决定加不加。
# 加列不影响已有数据，历史行的新列是 NULL —— 例如 location 加进来之后，
# 老通知的地点就是空的，需要人工补或重跑抽取。
_MIGRATIONS: list[tuple[str, str, str]] = [
    ("notification", "location", "TEXT"),
]


async def _migrate() -> None:
    for table, column, decl in _MIGRATIONS:
        async with db().execute(f"PRAGMA table_info({table})") as cur:
            existing = {row["name"] for row in await cur.fetchall()}
        if column not in existing:
            await db().execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
            logger.info("数据库迁移：%s 新增列 %s", table, column)
    await db().commit()


async def init_db() -> aiosqlite.Connection:
    global _conn
    if _conn is not None:
        return _conn
    settings = get_settings()
    settings.resolved_db_path.parent.mkdir(parents=True, exist_ok=True)
    _conn = await aiosqlite.connect(str(settings.resolved_db_path))
    _conn.row_factory = aiosqlite.Row
    await _conn.executescript(SCHEMA)
    await _conn.commit()
    await _migrate()
    return _conn


async def close_db() -> None:
    global _conn
    if _conn is not None:
        await _conn.close()
        _conn = None


def db() -> aiosqlite.Connection:
    if _conn is None:
        raise RuntimeError("数据库未初始化，请先 await init_db()")
    return _conn


# --------------------------------------------------------------------------
# 通用查询
# --------------------------------------------------------------------------


async def fetch_all(sql: str, params: Sequence[Any] = ()) -> list[dict]:
    async with db().execute(sql, params) as cur:
        rows = await cur.fetchall()
    return [dict(r) for r in rows]


async def fetch_one(sql: str, params: Sequence[Any] = ()) -> dict | None:
    async with db().execute(sql, params) as cur:
        row = await cur.fetchone()
    return dict(row) if row else None


async def execute(sql: str, params: Sequence[Any] = ()) -> int:
    """执行写操作，返回受影响行数。写操作串行化以避免 SQLite 锁冲突。"""
    async with _write_lock:
        cur = await db().execute(sql, params)
        await db().commit()
        return cur.rowcount


# --------------------------------------------------------------------------
# 原始层
# --------------------------------------------------------------------------


async def insert_raw_message(
    *,
    message_id: str,
    group_id: str,
    group_name: str | None,
    sender_id: str,
    sender_name: str | None,
    ts: int,
    content: str,
    attachments: list[dict],
    raw: dict,
) -> tuple[str, bool]:
    """写入原始消息。返回 (raw_id, 是否新插入)。重复消息不会覆盖已有记录。"""
    raw_id = new_id()
    cur = await execute(
        """
        INSERT OR IGNORE INTO raw_message
          (id, message_id, group_id, group_name, sender_id, sender_name, ts,
           content, attachments, raw, ingested_at, state)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,'pending')
        """,
        (
            raw_id,
            str(message_id),
            str(group_id),
            group_name,
            str(sender_id),
            sender_name,
            ts,
            content,
            json.dumps(attachments, ensure_ascii=False),
            json.dumps(raw, ensure_ascii=False),
            now_ms(),
        ),
    )
    if cur == 0:
        existing = await fetch_one(
            "SELECT id FROM raw_message WHERE group_id=? AND message_id=?",
            (str(group_id), str(message_id)),
        )
        return (existing["id"] if existing else raw_id), False
    await bump_stat("ingested")
    return raw_id, True


async def set_raw_state(raw_id: str, state: str, reason: str | None = None) -> None:
    await execute(
        "UPDATE raw_message SET state=?, state_reason=?, state_at=? WHERE id=?",
        (state, reason, now_ms(), raw_id),
    )


async def get_raw(raw_id: str) -> dict | None:
    return await fetch_one("SELECT * FROM raw_message WHERE id=?", (raw_id,))


# --------------------------------------------------------------------------
# 群状态 / 缺口检测
# --------------------------------------------------------------------------


async def touch_group(
    group_id: str, group_name: str | None, msg_ts: int
) -> dict | None:
    """更新群最后消息时间与今日计数。

    返回：如果本次写入暴露出一个时间缺口，返回缺口信息；否则 None。
    这是"宁可承认瞎了，也不假装正常"的实现点。
    """
    settings = get_settings()
    row = await fetch_one("SELECT * FROM group_state WHERE group_id=?", (str(group_id),))
    gap: dict | None = None
    today = local_day()

    if row and row.get("last_msg_ts"):
        prev_ts = int(row["last_msg_ts"])
        gap_ms = settings.gap_alert_hours * 3600 * 1000
        # 只对"新消息比上一条晚很多"的情况告警，乱序历史消息不告警
        if msg_ts - prev_ts > gap_ms:
            gap = {"group_id": str(group_id), "group_name": group_name, "from_ts": prev_ts, "to_ts": msg_ts}

    if row is None:
        await execute(
            """INSERT INTO group_state
               (group_id, group_name, last_msg_ts, last_msg_at, msg_count_today, count_date)
               VALUES (?,?,?,?,1,?)""",
            (str(group_id), group_name, msg_ts, now_ms(), today),
        )
    else:
        count = int(row.get("msg_count_today") or 0)
        if row.get("count_date") != today:
            count = 0
        await execute(
            """UPDATE group_state
               SET group_name=COALESCE(?, group_name),
                   last_msg_ts=MAX(COALESCE(last_msg_ts, 0), ?),
                   last_msg_at=?,
                   msg_count_today=?,
                   count_date=?
               WHERE group_id=?""",
            (group_name, msg_ts, now_ms(), count + 1, today, str(group_id)),
        )
    return gap


async def add_gap_alert(
    group_id: str, group_name: str | None, from_ts: int, to_ts: int, reason: str
) -> str:
    alert_id = new_id()
    await execute(
        """INSERT INTO gap_alert
           (id, group_id, group_name, from_ts, to_ts, reason, created_at)
           VALUES (?,?,?,?,?,?,?)""",
        (alert_id, str(group_id), group_name, from_ts, to_ts, reason, now_ms()),
    )
    return alert_id


async def list_gap_alerts(limit: int = 20) -> list[dict]:
    return await fetch_all(
        "SELECT * FROM gap_alert WHERE acknowledged=0 ORDER BY created_at DESC LIMIT ?",
        (limit,),
    )


async def list_group_states() -> list[dict]:
    return await fetch_all("SELECT * FROM group_state ORDER BY last_msg_ts DESC")


# --------------------------------------------------------------------------
# 派生层：notification
# --------------------------------------------------------------------------


async def upsert_notification(data: dict) -> str:
    existing = await fetch_one(
        "SELECT id FROM notification WHERE raw_message_id=?", (data["raw_message_id"],)
    )
    if existing:
        # 保留人工修正：只覆盖机器字段，correction 表不动
        await execute(
            """UPDATE notification SET
                 title=?, summary=?, location=?, due_at=?, due_text=?, due_confidence=?,
                 evidence=?, conflict=?, candidates=?, extractor=?, model=?,
                 prompt_ver=?, updated_at=?
               WHERE id=?""",
            (
                data["title"],
                data.get("summary"),
                data.get("location"),
                data.get("due_at"),
                data.get("due_text"),
                data.get("due_confidence", 0.0),
                data["evidence"],
                int(bool(data.get("conflict"))),
                json.dumps(data.get("candidates", []), ensure_ascii=False),
                data.get("extractor"),
                data.get("model"),
                data.get("prompt_ver"),
                now_ms(),
                existing["id"],
            ),
        )
        return existing["id"]

    notif_id = new_id()
    ts = now_ms()
    await execute(
        """INSERT INTO notification
           (id, raw_message_id, group_id, group_name, sender_id, sender_name, source_ts,
            title, summary, location, due_at, due_text, due_confidence, evidence, conflict,
            candidates, extractor, model, prompt_ver, created_at, updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            notif_id,
            data["raw_message_id"],
            str(data["group_id"]),
            data.get("group_name"),
            data.get("sender_id"),
            data.get("sender_name"),
            data["source_ts"],
            data["title"],
            data.get("summary"),
            data.get("location"),
            data.get("due_at"),
            data.get("due_text"),
            data.get("due_confidence", 0.0),
            data["evidence"],
            int(bool(data.get("conflict"))),
            json.dumps(data.get("candidates", []), ensure_ascii=False),
            data.get("extractor"),
            data.get("model"),
            data.get("prompt_ver"),
            ts,
            ts,
        ),
    )
    if data.get("conflict"):
        await bump_stat("conflicts")
    return notif_id


async def list_notifications(limit: int = 500) -> list[dict]:
    return await fetch_all(
        "SELECT * FROM notification ORDER BY due_at IS NULL, due_at ASC, source_ts DESC LIMIT ?",
        (limit,),
    )


async def get_notification(notif_id: str) -> dict | None:
    return await fetch_one("SELECT * FROM notification WHERE id=?", (notif_id,))


async def delete_notification_for_raw(raw_id: str) -> None:
    await execute("DELETE FROM notification WHERE raw_message_id=?", (raw_id,))


# --------------------------------------------------------------------------
# 人工修正 / 已读
# --------------------------------------------------------------------------


async def add_correction(notif_id: str, field: str, value: Any, user_id: str = "web") -> None:
    await execute(
        """INSERT INTO correction (id, notification_id, field, value, user_id, ts)
           VALUES (?,?,?,?,?,?)""",
        (new_id(), notif_id, field, None if value is None else str(value), user_id, now_ms()),
    )


async def corrections_for(notif_ids: Iterable[str]) -> dict[str, dict[str, Any]]:
    """取每个通知每个字段的**最新**修正值。"""
    ids = list(notif_ids)
    if not ids:
        return {}
    placeholders = ",".join("?" for _ in ids)
    rows = await fetch_all(
        f"""SELECT notification_id, field, value, ts FROM correction
            WHERE notification_id IN ({placeholders})
            ORDER BY ts ASC""",
        ids,
    )
    out: dict[str, dict[str, Any]] = {}
    for r in rows:
        out.setdefault(r["notification_id"], {})[r["field"]] = r["value"]
    return out


async def set_read(notif_id: str, read: bool) -> None:
    if read:
        await execute(
            "INSERT OR REPLACE INTO read_state (notification_id, read_at) VALUES (?,?)",
            (notif_id, now_ms()),
        )
    else:
        await execute("DELETE FROM read_state WHERE notification_id=?", (notif_id,))


async def read_map() -> set[str]:
    rows = await fetch_all("SELECT notification_id FROM read_state")
    return {r["notification_id"] for r in rows}


# --------------------------------------------------------------------------
# 统计 / digest 日志
# --------------------------------------------------------------------------


async def bump_stat(field: str, delta: int = 1, day: str | None = None) -> None:
    allowed = {"ingested", "extracted", "unparsed", "conflicts", "degraded", "llm_tokens"}
    if field not in allowed:
        raise ValueError(f"未知统计字段: {field}")
    d = day or local_day()
    await execute(
        f"""INSERT INTO pipeline_stat (day, {field}) VALUES (?, ?)
            ON CONFLICT(day) DO UPDATE SET {field} = {field} + excluded.{field}""",
        (d, delta),
    )


async def get_stat(day: str | None = None) -> dict:
    d = day or local_day()
    row = await fetch_one("SELECT * FROM pipeline_stat WHERE day=?", (d,))
    if row:
        return row
    return {
        "day": d,
        "ingested": 0,
        "extracted": 0,
        "unparsed": 0,
        "conflicts": 0,
        "degraded": 0,
        "llm_tokens": 0,
    }


async def log_digest(day: str, kind: str, text: str, sent: bool, error: str | None = None) -> str:
    digest_id = new_id()
    await execute(
        """INSERT INTO digest_log (id, day, kind, text, sent, error, ts)
           VALUES (?,?,?,?,?,?,?)""",
        (digest_id, day, kind, text, int(sent), error, now_ms()),
    )
    return digest_id


async def digest_sent_today(day: str) -> bool:
    row = await fetch_one(
        "SELECT id FROM digest_log WHERE day=? AND kind='auto' AND sent=1 LIMIT 1", (day,)
    )
    return row is not None


async def digest_auto_attempts_today(day: str) -> int:
    """当天自动发送的尝试次数。

    没有这个计数，发送失败后调度循环会每分钟重试一次，刷满日志表。
    """
    row = await fetch_one(
        "SELECT COUNT(*) AS c FROM digest_log WHERE day=? AND kind='auto'", (day,)
    )
    return int((row or {}).get("c") or 0)
