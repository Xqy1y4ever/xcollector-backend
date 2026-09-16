"""SQLite 数据访问层。

分层铁律（见 docs/design.md §3.1）：
  - raw_message 是唯一不可再生的资产，**只追加**，永不修改内容
    （只有 state* 三个状态列会更新，用于标记"这条处理到哪一步了"）
  - notification 是派生层，可整表重建
  - correction 是人工修正，**只追加**。展示时用 correction 覆盖 notification
    里对应字段，这样重跑永远不会覆盖人工修正。
  - attachment 的二进制在磁盘上（ATTACHMENT_DIR），库里只有元数据
  - digest_log（契约 §10）与 bot_state（契约 §11）是 bot 的外部记忆：
    后端只存字符串 / 任意 JSON，不理解含义，也不校验取值

这里只有增删查改，没有任何业务判断。什么算通知、该不该处理、DDL 对不对，
全部由 xcollector-bot 决定 —— 后端只负责存下来、再读回去。
"""

from __future__ import annotations

import asyncio
import json
import logging
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
  id            TEXT PRIMARY KEY,          -- 内部 ID（对外就是 raw_message_id）
  message_id    TEXT NOT NULL,             -- 来源侧的消息 ID，由 bot 提供
  group_id      TEXT NOT NULL,
  group_name    TEXT,
  sender_id     TEXT NOT NULL,
  sender_name   TEXT,
  ts            INTEGER NOT NULL,          -- 消息发送时间（毫秒）
  content       TEXT NOT NULL DEFAULT '',
  attachments   TEXT NOT NULL DEFAULT '[]',-- JSON 数组，原样透出
  raw           TEXT NOT NULL,             -- 来源侧原始 JSON，永不丢字段
  ingested_at   INTEGER NOT NULL,
  -- 处理进度标记：取值由 bot 定义，后端不校验、不理解
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
  location       TEXT,                     -- NULL = 原文没提（合法状态）
  due_at         INTEGER,                  -- NULL = 没解析出确定时间（合法状态）
  due_text       TEXT,
  due_confidence REAL NOT NULL DEFAULT 0,
  evidence       TEXT NOT NULL,            -- 支撑结论的原文片段，**必须非空**
  conflict       INTEGER NOT NULL DEFAULT 0,
  candidates     TEXT NOT NULL DEFAULT '[]',-- JSON 数组，原样透出
  extractor      TEXT,                     -- 溯源字段，取值由 bot 定义
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
  field           TEXT NOT NULL,
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

-- ==================== 群状态（bot 每收到一条消息 upsert 一次）====================
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

-- ==================== 统计计数（bot 自己数，后端只做累加）====================
CREATE TABLE IF NOT EXISTS pipeline_stat (
  day           TEXT PRIMARY KEY,
  ingested      INTEGER NOT NULL DEFAULT 0,
  extracted     INTEGER NOT NULL DEFAULT 0,
  unparsed      INTEGER NOT NULL DEFAULT 0,
  conflicts     INTEGER NOT NULL DEFAULT 0,
  degraded      INTEGER NOT NULL DEFAULT 0,
  llm_tokens    INTEGER NOT NULL DEFAULT 0
);

-- ==================== 附件元数据（二进制在 ATTACHMENT_DIR）====================
CREATE TABLE IF NOT EXISTS attachment (
  id           TEXT PRIMARY KEY,
  filename     TEXT,                       -- 清洗过的原始文件名，仅留档/下载显示
  stored_name  TEXT NOT NULL,              -- 实际落盘文件名，由 id 生成
  content_type TEXT,
  size         INTEGER NOT NULL DEFAULT 0,
  source_url   TEXT,                       -- 来源 URL，仅留档
  created_at   INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_att_created ON attachment(created_at DESC);

-- ==================== digest 发送记录（契约 §10）====================
CREATE TABLE IF NOT EXISTS digest_log (
  id      TEXT PRIMARY KEY,
  day     TEXT NOT NULL,
  kind    TEXT NOT NULL,                   -- 取值由 bot 定义，后端不校验
  text    TEXT NOT NULL DEFAULT '',
  sent    INTEGER NOT NULL DEFAULT 0,
  error   TEXT,
  ts      INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_digest_day ON digest_log(day, kind, ts DESC);

-- ==================== bot 的键值暂存（契约 §11）====================
CREATE TABLE IF NOT EXISTS bot_state (
  namespace  TEXT NOT NULL,
  "key"      TEXT NOT NULL,
  value      TEXT NOT NULL,                -- 任意 JSON，后端不理解含义
  expires_at INTEGER,                      -- NULL = 不过期（毫秒）
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL,
  PRIMARY KEY (namespace, "key")
);
CREATE INDEX IF NOT EXISTS ix_state_expires ON bot_state(expires_at);
"""

_conn: aiosqlite.Connection | None = None
_write_lock = asyncio.Lock()

# 增量迁移：(表, 列, 列定义)
# SQLite 没有 ADD COLUMN IF NOT EXISTS，只能先读表结构再决定加不加。
# 加列不影响已有数据，历史行的新列是 NULL —— 例如 location 加进来之后，
# 老通知的地点就是空的，需要人工补或重跑。
#
# 新增**表**不在这里登记：SCHEMA 里的 CREATE TABLE IF NOT EXISTS 每次启动都会执行，
# 老库会自动补出新表（attachment / digest_log / bot_state 都是这么加上去的）。
# 这里登记的是"老库已经有这张表、只是缺这一列"的情况；新表也顺手登记一遍，
# 保证万一日后给这张表加列时兼容路径已经在了。
_MIGRATIONS: list[tuple[str, str, str]] = [
    ("notification", "location", "TEXT"),
    ("attachment", "source_url", "TEXT"),
    ("digest_log", "error", "TEXT"),
    ("bot_state", "expires_at", "INTEGER"),
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


async def count_of(sql: str, params: Sequence[Any] = ()) -> int:
    row = await fetch_one(sql, params)
    return int((row or {}).get("c") or 0)


# --------------------------------------------------------------------------
# 原始层：raw_message
# --------------------------------------------------------------------------


def _message_filters(
    states: Iterable[str] | None = None,
    group_id: str | None = None,
    since: int | None = None,
) -> tuple[str, list[Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    state_list = [s for s in (states or []) if s]
    if state_list:
        clauses.append(f"state IN ({','.join('?' for _ in state_list)})")
        params.extend(state_list)
    if group_id:
        clauses.append("group_id = ?")
        params.append(str(group_id))
    if since is not None:
        clauses.append("ts >= ?")
        params.append(int(since))
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    return where, params


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
    """写入原始消息。返回 (raw_id, 是否新插入)。

    幂等键是 `(group_id, message_id)`：重复提交返回已有 id，不产生新行，
    也**不会覆盖**已有记录（原始层只追加）。
    """
    raw_id = new_id()
    rowcount = await execute(
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
            int(ts),
            content,
            json.dumps(attachments, ensure_ascii=False),
            json.dumps(raw, ensure_ascii=False),
            now_ms(),
        ),
    )
    if rowcount == 0:
        existing = await fetch_one(
            "SELECT id FROM raw_message WHERE group_id=? AND message_id=?",
            (str(group_id), str(message_id)),
        )
        return (existing["id"] if existing else raw_id), False
    return raw_id, True


# raw_message 上**唯一**允许改的列（契约 §1 的 PATCH）：
# content / raw / ts / message_id / group_id / sender_id 一律不可改 ——
# 那是消息本体，改了就破坏了"原始层只追加"这条铁律。
RAW_PATCHABLE = ("state", "state_reason", "attachments")


async def set_raw_state(raw_id: str, state: str, reason: str | None = None) -> bool:
    """只更新三个 state 列 —— 内容列永不修改（只追加铁律）。"""
    rowcount = await execute(
        "UPDATE raw_message SET state=?, state_reason=?, state_at=? WHERE id=?",
        (state, reason, now_ms(), raw_id),
    )
    return rowcount > 0


async def patch_raw_message(raw_id: str, fields: dict[str, Any]) -> bool:
    """改 state / state_reason / attachments（列名白名单在 RAW_PATCHABLE）。

    attachments 是"事后补齐"：bot 先落库消息本体，再去下载/上传附件，
    最后回填这张列表 —— 不是修改本体，所以允许。
    """
    unknown = [k for k in fields if k not in RAW_PATCHABLE]
    if unknown:
        raise ValueError(f"不可修改的字段：{unknown}")
    if not fields:
        return await get_raw(raw_id) is not None
    sets = ", ".join(f"{k}=?" for k in fields)
    params: list[Any] = list(fields.values())
    if "state" in fields:
        # 状态换了才更新状态时间；只补 attachments 不该动 state_at
        sets += ", state_at=?"
        params.append(now_ms())
    params.append(raw_id)
    return await execute(f"UPDATE raw_message SET {sets} WHERE id=?", params) > 0


async def get_raw(raw_id: str) -> dict | None:
    return await fetch_one("SELECT * FROM raw_message WHERE id=?", (raw_id,))


async def list_messages(
    *,
    states: Iterable[str] | None = None,
    group_id: str | None = None,
    since: int | None = None,
    limit: int = 100,
) -> list[dict]:
    where, params = _message_filters(states, group_id, since)
    return await fetch_all(
        f"SELECT * FROM raw_message{where} ORDER BY ts DESC, ingested_at DESC LIMIT ?",
        (*params, int(limit)),
    )


async def count_messages(
    *,
    states: Iterable[str] | None = None,
    group_id: str | None = None,
    since: int | None = None,
) -> int:
    where, params = _message_filters(states, group_id, since)
    return await count_of(f"SELECT COUNT(*) AS c FROM raw_message{where}", params)


# --------------------------------------------------------------------------
# 派生层：notification
# --------------------------------------------------------------------------

# 允许 PATCH 的机器字段（契约 §2）。status / read 刻意不在里面：
# 它们必须走 corrections 与 /read 留痕。
NOTIFICATION_PATCHABLE = (
    "title",
    "summary",
    "location",
    "due_at",
    "due_text",
    "due_confidence",
    "evidence",
    "conflict",
    "candidates",
    "model",
    "prompt_ver",
)


def _notification_machine_values(data: dict) -> tuple:
    return (
        data.get("title"),
        data.get("summary"),
        data.get("location"),
        data.get("due_at"),
        data.get("due_text"),
        data.get("due_confidence", 0.0),
        data.get("evidence"),
        int(bool(data.get("conflict"))),
        json.dumps(data.get("candidates", []), ensure_ascii=False),
        data.get("extractor"),
        data.get("model"),
        data.get("prompt_ver"),
    )


async def upsert_notification(data: dict) -> tuple[str, bool]:
    """按 `raw_message_id` 幂等写入通知。返回 (notif_id, 是否新建)。

    已存在时只覆盖**机器字段**，correction 表不动 —— 于是"重跑抽取"
    永远不会冲掉人工修正（tests/check_corrections.py 守的就是这条）。
    """
    existing = await fetch_one(
        "SELECT id FROM notification WHERE raw_message_id=?", (data["raw_message_id"],)
    )
    values = _notification_machine_values(data)
    if existing:
        await execute(
            """UPDATE notification SET
                 title=?, summary=?, location=?, due_at=?, due_text=?, due_confidence=?,
                 evidence=?, conflict=?, candidates=?, extractor=?, model=?,
                 prompt_ver=?, updated_at=?
               WHERE id=?""",
            (*values, now_ms(), existing["id"]),
        )
        return existing["id"], False

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
            int(data["source_ts"]),
            *values,
            ts,
            ts,
        ),
    )
    return notif_id, True


async def get_notification_row(notif_id: str) -> dict | None:
    return await fetch_one("SELECT * FROM notification WHERE id=?", (notif_id,))


async def patch_notification(notif_id: str, fields: dict[str, Any]) -> bool:
    """改机器字段。`fields` 的键必须来自 NOTIFICATION_PATCHABLE（列名白名单）。"""
    unknown = [k for k in fields if k not in NOTIFICATION_PATCHABLE]
    if unknown:
        raise ValueError(f"不可修改的字段：{unknown}")
    if not fields:
        return await get_notification_row(notif_id) is not None
    sets = ", ".join(f"{k}=?" for k in fields)
    rowcount = await execute(
        f"UPDATE notification SET {sets}, updated_at=? WHERE id=?",
        (*fields.values(), now_ms(), notif_id),
    )
    return rowcount > 0


async def touch_notification(notif_id: str) -> None:
    """把 updated_at 推到当前时间。

    人工修正与已读都会改变读投影，`since` 是"有没有变过"的游标，
    所以它们也要推一下 —— 否则前端增量同步会漏掉这些变化。
    """
    await execute("UPDATE notification SET updated_at=? WHERE id=?", (now_ms(), notif_id))


async def delete_notification(notif_id: str) -> bool:
    """删除通知行。correction / read_state 保留（只追加层不做级联删除）。"""
    return await execute("DELETE FROM notification WHERE id=?", (notif_id,)) > 0


async def count_notifications() -> int:
    return await count_of("SELECT COUNT(*) AS c FROM notification")


# --------------------------------------------------------------------------
# 人工修正 / 已读
# --------------------------------------------------------------------------


async def add_correction(
    notif_id: str, field: str, value: Any, user_id: str = "web"
) -> str:
    corr_id = new_id()
    await execute(
        """INSERT INTO correction (id, notification_id, field, value, user_id, ts)
           VALUES (?,?,?,?,?,?)""",
        (corr_id, notif_id, field, None if value is None else str(value), user_id, now_ms()),
    )
    await touch_notification(notif_id)
    return corr_id


async def list_corrections(notif_id: str) -> list[dict]:
    """修正历史，按时间正序（契约 §2）。"""
    return await fetch_all(
        """SELECT id, notification_id, field, value, user_id, ts FROM correction
           WHERE notification_id=? ORDER BY ts ASC, id ASC""",
        (notif_id,),
    )


async def set_read(notif_id: str, read: bool) -> None:
    if read:
        await execute(
            "INSERT OR REPLACE INTO read_state (notification_id, read_at) VALUES (?,?)",
            (notif_id, now_ms()),
        )
    else:
        await execute("DELETE FROM read_state WHERE notification_id=?", (notif_id,))
    await touch_notification(notif_id)


# --------------------------------------------------------------------------
# 群状态
# --------------------------------------------------------------------------


async def upsert_group(
    group_id: str, group_name: str | None, last_msg_ts: int
) -> dict:
    """upsert 群状态，并把**更新前**的 last_msg_ts 一并返回。

    bot 的缺口检测需要"上一条消息的时间"；在响应里一起给回去，
    就省掉了"先读再写"那一次竞态。
    """
    gid = str(group_id)
    ts = int(last_msg_ts)
    today = local_day()
    row = await fetch_one("SELECT * FROM group_state WHERE group_id=?", (gid,))
    previous = None
    if row is not None and row.get("last_msg_ts") is not None:
        previous = int(row["last_msg_ts"])

    stamp = now_ms()
    if row is None:
        await execute(
            """INSERT INTO group_state
               (group_id, group_name, last_msg_ts, last_msg_at, msg_count_today, count_date)
               VALUES (?,?,?,?,1,?)""",
            (gid, group_name, ts, stamp, today),
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
            (group_name, ts, stamp, count + 1, today, gid),
        )

    fresh = await fetch_one("SELECT * FROM group_state WHERE group_id=?", (gid,))
    return {"group": fresh, "previous_last_msg_ts": previous}


async def list_groups() -> list[dict]:
    return await fetch_all("SELECT * FROM group_state ORDER BY last_msg_ts DESC")


# --------------------------------------------------------------------------
# 缺口告警
# --------------------------------------------------------------------------


async def add_gap_alert(
    group_id: str,
    group_name: str | None,
    from_ts: int,
    to_ts: int,
    reason: str | None,
) -> str:
    alert_id = new_id("gap_")
    await execute(
        """INSERT INTO gap_alert
           (id, group_id, group_name, from_ts, to_ts, reason, created_at, acknowledged)
           VALUES (?,?,?,?,?,?,?,0)""",
        (alert_id, str(group_id), group_name, int(from_ts), int(to_ts), reason, now_ms()),
    )
    return alert_id


async def list_gap_alerts(
    *, acknowledged: bool | None = None, limit: int = 20
) -> list[dict]:
    where = ""
    params: list[Any] = []
    if acknowledged is not None:
        where = " WHERE acknowledged=?"
        params.append(1 if acknowledged else 0)
    return await fetch_all(
        f"SELECT * FROM gap_alert{where} ORDER BY created_at DESC LIMIT ?",
        (*params, int(limit)),
    )


async def ack_gap_alert(alert_id: str) -> bool:
    return (
        await execute("UPDATE gap_alert SET acknowledged=1 WHERE id=?", (alert_id,)) > 0
    )


# --------------------------------------------------------------------------
# 统计：后端只做累加
# --------------------------------------------------------------------------

STAT_FIELDS = ("ingested", "extracted", "unparsed", "conflicts", "degraded", "llm_tokens")


async def add_stats(day: str | None, fields: dict[str, int]) -> dict:
    """把 `fields` 里的计数累加到 `day` 那一行，返回累加后的整行。

    字段含义后端一概不理解：来的是已知列就加，未知键忽略（契约：未知字段忽略）。
    """
    d = (day or "").strip() or local_day()
    increments = {k: int(v) for k, v in fields.items() if k in STAT_FIELDS}
    await execute("INSERT OR IGNORE INTO pipeline_stat (day) VALUES (?)", (d,))
    if increments:
        sets = ", ".join(f"{k} = {k} + ?" for k in increments)
        await execute(
            f"UPDATE pipeline_stat SET {sets} WHERE day=?", (*increments.values(), d)
        )
    return await get_stat(d)


async def get_stat(day: str | None = None) -> dict:
    d = (day or "").strip() or local_day()
    row = await fetch_one("SELECT * FROM pipeline_stat WHERE day=?", (d,))
    return {"day": d, **{f: int((row or {}).get(f) or 0) for f in STAT_FIELDS}}


# --------------------------------------------------------------------------
# 附件元数据（二进制不在这里）
# --------------------------------------------------------------------------


async def insert_attachment(
    *,
    att_id: str,
    filename: str | None,
    stored_name: str,
    content_type: str | None,
    size: int,
    source_url: str | None,
) -> None:
    await execute(
        """INSERT INTO attachment
           (id, filename, stored_name, content_type, size, source_url, created_at)
           VALUES (?,?,?,?,?,?,?)""",
        (att_id, filename, stored_name, content_type, int(size), source_url, now_ms()),
    )


async def get_attachment(att_id: str) -> dict | None:
    return await fetch_one("SELECT * FROM attachment WHERE id=?", (att_id,))


async def count_attachments() -> int:
    return await count_of("SELECT COUNT(*) AS c FROM attachment")


# --------------------------------------------------------------------------
# digest 发送记录（契约 §10）：后端只记，不理解 kind 的含义
# --------------------------------------------------------------------------


async def add_digest_log(
    *,
    day: str | None,
    kind: str,
    text: str,
    sent: bool,
    error: str | None,
) -> tuple[str, bool]:
    """写入一条发送记录，返回 (id, 是否新写入)。

    幂等键是 `(day, kind, sent)`：同一天同一 kind 同一结果重复提交返回已有 id。
    这样 bot 重启/重试不会把"今天发过没有"的答案搞成 2 —— 对收件人来说
    重发一条摘要比漏发更糟，所以这里的幂等是刻意的。
    """
    d = (day or "").strip() or local_day()
    sent_int = 1 if sent else 0
    log_id = new_id()
    rowcount = await execute(
        """INSERT INTO digest_log (id, day, kind, text, sent, error, ts)
           SELECT ?,?,?,?,?,?,?
           WHERE NOT EXISTS (
             SELECT 1 FROM digest_log WHERE day=? AND kind=? AND sent=?
           )""",
        (log_id, d, kind, text, sent_int, error, now_ms(), d, kind, sent_int),
    )
    if rowcount == 0:
        existing = await fetch_one(
            """SELECT id FROM digest_log WHERE day=? AND kind=? AND sent=?
               ORDER BY ts ASC, id ASC LIMIT 1""",
            (d, kind, sent_int),
        )
        return (existing["id"] if existing else log_id), False
    return log_id, True


def _digest_log_filters(
    day: str | None, kind: str | None, sent: bool | None
) -> tuple[str, list[Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    if day:
        clauses.append("day = ?")
        params.append(str(day))
    if kind:
        clauses.append("kind = ?")
        params.append(str(kind))
    if sent is not None:
        clauses.append("sent = ?")
        params.append(1 if sent else 0)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    return where, params


async def list_digest_logs(
    *,
    day: str | None = None,
    kind: str | None = None,
    sent: bool | None = None,
    limit: int = 50,
) -> list[dict]:
    where, params = _digest_log_filters(day, kind, sent)
    return await fetch_all(
        f"SELECT * FROM digest_log{where} ORDER BY ts DESC, id DESC LIMIT ?",
        (*params, int(limit)),
    )


async def count_digest_logs(
    *, day: str | None = None, kind: str | None = None, sent: bool | None = None
) -> int:
    where, params = _digest_log_filters(day, kind, sent)
    return await count_of(f"SELECT COUNT(*) AS c FROM digest_log{where}", params)


# --------------------------------------------------------------------------
# bot 的键值暂存（契约 §11）
#
# 过期判定**在读取时**做：物理清理只是省空间，正确性不建立"有个后台任务会删"上。
# --------------------------------------------------------------------------


def _state_expired(row: dict, now: int) -> bool:
    expires_at = row.get("expires_at")
    return expires_at is not None and int(expires_at) <= now


async def put_state(
    namespace: str, key: str, value_json: str, expires_at: int | None = None
) -> dict:
    stamp = now_ms()
    await execute(
        """INSERT INTO bot_state (namespace, "key", value, expires_at, created_at, updated_at)
           VALUES (?,?,?,?,?,?)
           ON CONFLICT(namespace, "key") DO UPDATE SET
             value=excluded.value,
             expires_at=excluded.expires_at,
             updated_at=excluded.updated_at""",
        (namespace, key, value_json, expires_at, stamp, stamp),
    )
    return {"namespace": namespace, "key": key, "expires_at": expires_at}


async def get_state(
    namespace: str, key: str, *, now: int | None = None
) -> dict | None:
    """取一个键；已过期视为不存在（并顺手删掉）。"""
    moment = now_ms() if now is None else now
    row = await fetch_one(
        'SELECT * FROM bot_state WHERE namespace=? AND "key"=?', (namespace, key)
    )
    if row is None:
        return None
    if _state_expired(row, moment):
        await delete_state(namespace, key)
        return None
    return row


async def delete_state(namespace: str, key: str) -> bool:
    return (
        await execute(
            'DELETE FROM bot_state WHERE namespace=? AND "key"=?', (namespace, key)
        )
        > 0
    )


async def list_state(
    namespace: str, *, limit: int | None = None, now: int | None = None
) -> list[dict]:
    """列出一个 namespace 下**未过期**的键值。"""
    moment = now_ms() if now is None else now
    sql = (
        'SELECT * FROM bot_state WHERE namespace=? AND (expires_at IS NULL OR expires_at > ?)'
        ' ORDER BY updated_at DESC, "key" ASC'
    )
    params: list[Any] = [namespace, moment]
    if limit is not None:
        sql += " LIMIT ?"
        params.append(int(limit))
    rows = await fetch_all(sql, params)
    await purge_expired_state(namespace, now=moment)
    return rows


async def purge_expired_state(namespace: str | None = None, *, now: int | None = None) -> int:
    """顺手清掉过期行 —— 只是省空间，读取路径不依赖它。"""
    moment = now_ms() if now is None else now
    if namespace is None:
        return await execute(
            "DELETE FROM bot_state WHERE expires_at IS NOT NULL AND expires_at <= ?",
            (moment,),
        )
    return await execute(
        'DELETE FROM bot_state WHERE namespace=? AND expires_at IS NOT NULL AND expires_at <= ?',
        (namespace, moment),
    )
