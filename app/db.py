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
import re
from typing import Any, Iterable, Sequence

import aiosqlite

from .config import get_settings
from .utils import local_day, new_id, now_ms

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 需要**重建**（改主键 / 唯一约束）的三张表。
#
# SQLite 改不了主键和 UNIQUE，只能建新表搬数据。搬的时候必须用**和 SCHEMA 完全
# 同一份**建表语句 —— 所以抽成常量，两边都引用它。抄第二份的下场是：某天给表
# 加了一列，只改了 SCHEMA，老库重建出来的表少一列，而且不会有任何报错。
#
# `{table}` 是占位符：SCHEMA 里填真名，重建时填临时表名。
# 索引不在常量里 —— 它们由 SCHEMA 负责，重建完再跑一遍 SCHEMA 就补回来了。
# ---------------------------------------------------------------------------

_CREATE_NOTIFICATION = """CREATE TABLE IF NOT EXISTS {table} (
  id             TEXT PRIMARY KEY,
  user_id        TEXT NOT NULL,
  raw_message_id TEXT NOT NULL,
  group_id       TEXT NOT NULL,
  group_name     TEXT,
  sender_id      TEXT,
  sender_name    TEXT,
  source_ts      INTEGER NOT NULL,
  title          TEXT NOT NULL,
  summary        TEXT,
  location       TEXT,
  due_at         INTEGER,
  due_text       TEXT,
  due_confidence REAL NOT NULL DEFAULT 0,
  evidence       TEXT NOT NULL,
  conflict       INTEGER NOT NULL DEFAULT 0,
  candidates     TEXT NOT NULL DEFAULT '[]',
  extractor      TEXT,
  model          TEXT,
  prompt_ver     TEXT,
  created_at     INTEGER NOT NULL,
  updated_at     INTEGER NOT NULL,
  UNIQUE(user_id, raw_message_id)
)"""

_CREATE_PIPELINE_STAT = """CREATE TABLE IF NOT EXISTS {table} (
  user_id       TEXT NOT NULL,
  day           TEXT NOT NULL,
  ingested      INTEGER NOT NULL DEFAULT 0,
  extracted     INTEGER NOT NULL DEFAULT 0,
  unparsed      INTEGER NOT NULL DEFAULT 0,
  conflicts     INTEGER NOT NULL DEFAULT 0,
  degraded      INTEGER NOT NULL DEFAULT 0,
  llm_tokens    INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (user_id, day)
)"""

_CREATE_BOT_STATE = """CREATE TABLE IF NOT EXISTS {table} (
  user_id    TEXT NOT NULL,
  namespace  TEXT NOT NULL,
  "key"      TEXT NOT NULL,
  value      TEXT NOT NULL,
  expires_at INTEGER,
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL,
  PRIMARY KEY (user_id, namespace, "key")
)"""

# (表名, 建表语句, 目标列顺序)。重建后 null 填充新增列。
_REBUILDS: list[tuple[str, str, list[str]]] = [
    (
        "notification",
        _CREATE_NOTIFICATION,
        [
            "id", "user_id", "raw_message_id", "group_id", "group_name",
            "sender_id", "sender_name", "source_ts", "title", "summary",
            "location", "due_at", "due_text", "due_confidence", "evidence",
            "conflict", "candidates", "extractor", "model", "prompt_ver",
            "created_at", "updated_at",
        ],
    ),
    (
        "pipeline_stat",
        _CREATE_PIPELINE_STAT,
        [
            "user_id", "day", "ingested", "extracted", "unparsed",
            "conflicts", "degraded", "llm_tokens",
        ],
    ),
    (
        "bot_state",
        _CREATE_BOT_STATE,
        [
            "user_id", "namespace", "key", "value",
            "expires_at", "created_at", "updated_at",
        ],
    ),
]

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
-- 多用户之后，通知是**按用户扇出**的：同一个 raw_message 被 N 个用户订阅，
-- 就有 N 行 notification，每行各自带读/完成/修正状态。
-- 所以唯一约束从 (raw_message_id) 变成 (user_id, raw_message_id)。
""" + _CREATE_NOTIFICATION.format(table="notification") + """;
CREATE INDEX IF NOT EXISTS ix_notif_owner   ON notification(user_id, updated_at DESC);
CREATE INDEX IF NOT EXISTS ix_notif_due     ON notification(user_id, due_at);
CREATE INDEX IF NOT EXISTS ix_notif_updated ON notification(updated_at DESC);
CREATE INDEX IF NOT EXISTS ix_notif_group   ON notification(group_id, source_ts DESC);

-- ==================== 人工修正：只追加 ====================
-- 两个 user 字段含义不同，别混：
--   user_id  租户 —— 这条修正属于谁的数据（隔离用）
--   actor    谁操作的 —— 界面上显示"谁改的"（QQ 号 / "web"）
CREATE TABLE IF NOT EXISTS correction (
  id              TEXT PRIMARY KEY,
  user_id         TEXT NOT NULL,           -- 租户
  notification_id TEXT NOT NULL,
  field           TEXT NOT NULL,
  value           TEXT,                    -- 统一存字符串，读取时按字段类型还原
  actor           TEXT,                    -- 操作者（旧列名叫 user_id，已改名）
  ts              INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_corr_notif ON correction(notification_id, field, ts);
CREATE INDEX IF NOT EXISTS ix_corr_owner ON correction(user_id, notification_id);

-- ==================== 已读状态 ====================
-- notification_id 本身已经是"某个用户的那一条"，所以主键不用变；
-- user_id 是**冗余**出来的，作用只有一个：让"所有用户表都必须带 user_id"
-- 这条自检规则没有例外（例外会被人当成"这里可以不带"）。
CREATE TABLE IF NOT EXISTS read_state (
  notification_id TEXT PRIMARY KEY,
  user_id         TEXT NOT NULL,
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
-- 按用户扇出：用户只该看到自己订阅的群的缺口，否则是信息泄露。
CREATE TABLE IF NOT EXISTS gap_alert (
  id           TEXT PRIMARY KEY,
  user_id      TEXT NOT NULL,
  group_id     TEXT NOT NULL,
  group_name   TEXT,
  from_ts      INTEGER NOT NULL,
  to_ts        INTEGER NOT NULL,
  reason       TEXT,
  created_at   INTEGER NOT NULL,
  acknowledged INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_gap_created ON gap_alert(user_id, created_at DESC);

-- ==================== 统计计数（bot 自己数，后端只做累加）====================
-- 按用户分开统计：状态页给用户看的是"你的流水线"，不是全站。
""" + _CREATE_PIPELINE_STAT.format(table="pipeline_stat") + """;

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
  user_id TEXT NOT NULL,                   -- 发给谁的那一份
  day     TEXT NOT NULL,
  kind    TEXT NOT NULL,                   -- 取值由 bot 定义，后端不校验
  text    TEXT NOT NULL DEFAULT '',
  sent    INTEGER NOT NULL DEFAULT 0,
  error   TEXT,
  ts      INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_digest_day ON digest_log(user_id, day, kind, ts DESC);

-- ==================== bot 的键值暂存（契约 §11）====================
-- 「这个用户还挂着什么没做完」必须按用户分开：两个用户同时 /add 待确认，
-- 不能互相覆盖。
""" + _CREATE_BOT_STATE.format(table="bot_state") + """;
CREATE INDEX IF NOT EXISTS ix_state_expires ON bot_state(expires_at);

-- ==================== 用户与注册 ====================
-- 多用户服务的地基。三张表各管一件事：
--   app_user          谁是用户、他的令牌是什么
--   invite_code       谁能注册（邀请码/审批制，见 config.signup_mode）
--   qq_verify_code    「这个 QQ 确实是你的」怎么证明
--
-- **令牌只存 sha256**：库被拿走也不等于所有人的令牌被拿走。代价是令牌只能
-- 在注册/重置那一次显示给用户，之后再也拿不回来（和 API key 一个道理）。
CREATE TABLE IF NOT EXISTS app_user (
  id           TEXT PRIMARY KEY,             -- usr_xxx
  qq           TEXT NOT NULL UNIQUE,         -- 身份锚点：注册、找回令牌都靠它
  display_name TEXT,
  token_hash   TEXT NOT NULL,                -- UserToken 的 sha256（不存明文）
  token_hint   TEXT NOT NULL DEFAULT '',     -- 令牌前 8 位，只用来让用户认出是哪一个
  status       TEXT NOT NULL DEFAULT 'active',  -- active / disabled
  created_at   INTEGER NOT NULL,
  last_seen_at INTEGER
);
CREATE INDEX IF NOT EXISTS ix_user_status ON app_user(status);

CREATE TABLE IF NOT EXISTS invite_code (
  code       TEXT PRIMARY KEY,
  note       TEXT,                           -- 发给谁用的，纯备注
  max_uses   INTEGER NOT NULL DEFAULT 1,
  used_count INTEGER NOT NULL DEFAULT 0,
  expires_at INTEGER,                        -- NULL = 不过期
  created_at INTEGER NOT NULL
);

-- 一个 QQ 同时只留一个待用验证码（重新申请就覆盖旧的）
CREATE TABLE IF NOT EXISTS qq_verify_code (
  qq         TEXT PRIMARY KEY,
  code       TEXT NOT NULL,
  expires_at INTEGER NOT NULL,
  attempts   INTEGER NOT NULL DEFAULT 0,     -- 猜错次数，超上限直接作废
  created_at INTEGER NOT NULL
);
-- ==================== 用户的订阅（契约 §12）====================
-- 一个用户订的是「**谁**在**哪个群**说的话」，不是「哪个群」。
--
-- 为什么最小单位是 (群, 发送者) 而不是群：这条流水线里进清单的东西是**人**发的，
-- 不是群发的。允许订"整个群"就等于允许"这个群里任何人说话都进我的清单" ——
-- 那正是要避免的噪声，而且一旦有人这么订了，LLM 的调用量和误报都会失控。
-- 所以 sender_id 在库层面 NOT NULL，写入路径也拒绝空值和通配符（见 subscriptions.py）。
--
-- 订阅定义的是"**抽什么**"；bot 的群白名单定义的是"**看得到什么**"。两者都要满足。
CREATE TABLE IF NOT EXISTS subscription (
  id          TEXT PRIMARY KEY,
  user_id     TEXT NOT NULL,
  group_id    TEXT NOT NULL,
  sender_id   TEXT NOT NULL,               -- 必填，刻意不给"整个群"留口子
  group_name  TEXT,                        -- 冗余存名字，只是为了列表好看
  sender_name TEXT,
  note        TEXT,                        -- 用户自己的备注
  enabled     INTEGER NOT NULL DEFAULT 1,  -- 关掉但留着，比删了再重建友好
  created_at  INTEGER NOT NULL,
  updated_at  INTEGER NOT NULL,
  UNIQUE (user_id, group_id, sender_id)
);
CREATE INDEX IF NOT EXISTS ix_sub_owner ON subscription(user_id, enabled, updated_at DESC);
-- 给 bot 的路由查询用：来了一条 (群, 发送者) 的消息，谁要？
CREATE INDEX IF NOT EXISTS ix_sub_route ON subscription(group_id, sender_id, enabled);
"""

_conn: aiosqlite.Connection | None = None
_write_lock = asyncio.Lock()

# 增量迁移：(表, 列, 列定义)
# SQLite 没有 ADD COLUMN IF NOT EXISTS，只能先读表结构再决定加不加。
# 加列不影响已有数据，历史行的新列是 NULL —— 例如 location 加进来之后，
# 老通知的地点就是空的，需要人工补或重跑。
#
# 新增**表**不在这里登记：SCHEMA 里的 CREATE TABLE IF NOT EXISTS 每次启动都会执行，
# 老库会自动补出新表。这里登记的是"老库已经有这张表、只是缺这一列"的情况。
_MIGRATIONS: list[tuple[str, str, str]] = [
    ("notification", "location", "TEXT"),
    ("attachment", "source_url", "TEXT"),
    ("digest_log", "error", "TEXT"),
    ("bot_state", "expires_at", "INTEGER"),
    # ---- 多用户改造（Phase 2）：按用户隔离 ----
    ("correction", "user_id", "TEXT"),
    ("read_state", "user_id", "TEXT"),
    ("gap_alert", "user_id", "TEXT"),
    ("digest_log", "user_id", "TEXT"),
]

# 列改名：(表, 旧名, 新名)
#
# correction 原来就用 `user_id` 表示"谁做的修正"（QQ 号 / "web"）。现在
# `user_id` 必须留给**租户**，所以旧的那个改名叫 `actor`。
# ⚠️ 顺序要紧：必须**先改名再新增** user_id，否则新列加不进来（同名冲突）。
# 判据用"新名在不在"而不是"旧名在不在"：新库建出来就是 actor，重跑不会误改。
_RENAMES: list[tuple[str, str, str]] = [
    ("correction", "user_id", "actor"),
]


async def _table_columns(table: str) -> set[str]:
    async with db().execute(f"PRAGMA table_info({table})") as cur:
        return {row["name"] for row in await cur.fetchall()}


async def _table_exists(table: str) -> bool:
    row = await fetch_one(
        "SELECT 1 AS ok FROM sqlite_master WHERE type='table' AND name = ?", (table,)
    )
    return row is not None


async def _rebuild_table(table: str, create_sql: str, target: list[str]) -> None:
    """换掉表结构（改主键 / 唯一约束只能这么干）。

    旧表里没有的列填 NULL —— 多用户改造加进来的 `user_id` 因此是 NULL，
    也就是**老数据不属于任何用户、对谁都不可见**。这是刻意的：把无主数据
    随便分给某个人，比让它看不见危险得多。
    """
    tmp = f"{table}__migrate"
    existing = await _table_columns(table)

    await db().executescript(f"DROP TABLE IF EXISTS {tmp};")
    await db().executescript(create_sql.format(table=tmp))

    cols = ", ".join(f'"{c}"' for c in target)
    select = ", ".join(f'"{c}"' if c in existing else "NULL" for c in target)
    await db().execute(f'INSERT INTO "{tmp}" ({cols}) SELECT {select} FROM "{table}"')
    moved = await count_of(f'SELECT COUNT(*) AS n FROM "{tmp}"')

    await db().execute(f'DROP TABLE "{table}"')
    await db().execute(f'ALTER TABLE "{tmp}" RENAME TO "{table}"')
    logger.info(
        "数据库迁移：重建表 %s（%d 行），新增列 %s",
        table,
        moved,
        [c for c in target if c not in existing],
    )


async def _migrate() -> None:
    rebuilt = False

    for table, old, new in _RENAMES:
        if not await _table_exists(table):
            continue
        cols = await _table_columns(table)
        if new not in cols and old in cols:
            await db().execute(f'ALTER TABLE "{table}" RENAME COLUMN "{old}" TO "{new}"')
            logger.info("数据库迁移：%s 列 %s 改名为 %s", table, old, new)

    for table, column, decl in _MIGRATIONS:
        if not await _table_exists(table):
            continue
        if column not in await _table_columns(table):
            await db().execute(f'ALTER TABLE "{table}" ADD COLUMN "{column}" {decl}')
            logger.info("数据库迁移：%s 新增列 %s", table, column)

    # 重建必须放在加列**之后**：重建时要按目标列名去旧表里取值，
    # 先把能加的列加上，能保留的数据才最多。
    for table, create_sql, target in _REBUILDS:
        if not await _table_exists(table):
            continue
        if "user_id" in await _table_columns(table):
            continue  # 已经是新结构
        await _rebuild_table(table, create_sql, target)
        rebuilt = True

    await db().commit()

    if rebuilt:
        # 重建时旧表被 DROP，挂在它上面的索引一起没了。再跑一遍 SCHEMA 补回来
        # （CREATE TABLE / INDEX IF NOT EXISTS 对已存在的都是空操作）。
        await db().executescript(SCHEMA)
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
# 按用户隔离的表 + 运行时护栏
#
# 多用户服务最严重的 bug 不是崩溃，是**串数据**：用户 A 看到用户 B 的通知。
# 靠"写代码时小心"守不住 —— 30 多个查询，漏一个就是泄漏，而且不会有任何报错。
#
# 所以这里做一道运行时检查：任何 SQL 只要碰了下面这些表，就必须出现 `user_id`，
# 否则直接抛异常。**所有环境都开着**，不是只在测试里 —— 一个关掉的护栏等于没有。
#
# 不算"用户表"的：raw_message（所有人共享的并集）、attachment（字节只存一份，
# 访问权由它所属的通知决定）、group_state（群级事实）、app_user 等注册相关表
# （它们本来就是按 qq / id 定位的）。
# --------------------------------------------------------------------------

USER_SCOPED_TABLES = frozenset(
    {
        "notification",
        "correction",
        "read_state",
        "gap_alert",
        "pipeline_stat",
        "digest_log",
        "bot_state",
        "subscription",
    }
)

# 只看 FROM / INTO / UPDATE / JOIN 后面的那个词 —— 这样 `notification_id`
# 这类列名不会被误判成表名。
_SQL_TABLE_RE = re.compile(r"\b(?:FROM|INTO|UPDATE|JOIN)\s+\"?(\w+)\"?", re.IGNORECASE)


class UnscopedQueryError(RuntimeError):
    """碰了用户表，却没有按 user_id 过滤。"""


def assert_scoped(sql: str) -> None:
    touched = {m.group(1).lower() for m in _SQL_TABLE_RE.finditer(sql)}
    leaked = touched & USER_SCOPED_TABLES
    if leaked and "user_id" not in sql.lower():
        raise UnscopedQueryError(
            f"查询碰了用户表 {sorted(leaked)} 却没有 user_id 条件 —— "
            f"这会让用户看到别人的数据：\n{sql.strip()[:300]}"
        )


# --------------------------------------------------------------------------
# 通用查询
# --------------------------------------------------------------------------


async def fetch_all(sql: str, params: Sequence[Any] = ()) -> list[dict]:
    assert_scoped(sql)
    async with db().execute(sql, params) as cur:
        rows = await cur.fetchall()
    return [dict(r) for r in rows]


async def fetch_one(sql: str, params: Sequence[Any] = ()) -> dict | None:
    assert_scoped(sql)
    async with db().execute(sql, params) as cur:
        row = await cur.fetchone()
    return dict(row) if row else None


async def execute(sql: str, params: Sequence[Any] = ()) -> int:
    """执行写操作，返回受影响行数。写操作串行化以避免 SQLite 锁冲突。"""
    assert_scoped(sql)
    return await _execute_unscoped(sql, params)


async def _execute_unscoped(sql: str, params: Sequence[Any] = ()) -> int:
    """**绕过护栏**的写操作。只给明确知道自己在干什么的地方用（目前只有全局过期清理）。

    单独立一个函数而不是加个开关参数：绕过得在代码里看得见，
    这样 review 的时候一眼能数出有几处、为什么。
    """
    async with _write_lock:
        cur = await db().execute(sql, params)
        await db().commit()
        return cur.rowcount


async def _fetch_all_unscoped(sql: str, params: Sequence[Any] = ()) -> list[dict]:
    """**绕过护栏**的读操作。目前只有一处：bot 的路由查询 `find_subscribers`。

    为什么它必须绕过：来了一条 (群, 发送者) 的消息，要问的是"**所有**用户里
    谁订了这个来源" —— 这条查询按定义就不带 user_id，但它返回的是**投递名单**，
    不是任何人的数据。真正要防的"用户看到别人的数据"在这里不成立：
    调用方是服务令牌（bot），而且返回的 user_id 会立刻被用来各自的扇出。

    除了这里，任何跨用户读都必须走带 user_id 的路径。
    """
    async with db().execute(sql, params) as cur:
        rows = await cur.fetchall()
    return [dict(r) for r in rows]


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


async def upsert_notification(user_id: str, data: dict) -> tuple[str, bool]:
    """按 `(user_id, raw_message_id)` 幂等写入通知。返回 (notif_id, 是否新建)。

    已存在时只覆盖**机器字段**，correction 表不动 —— 于是"重跑抽取"
    永远不会冲掉人工修正（tests/check_corrections.py 守的就是这条）。

    同一个 raw_message 会被扇出成 N 行（每个订阅它的用户一行），
    所以幂等键必须带上 user_id。
    """
    existing = await fetch_one(
        "SELECT id FROM notification WHERE user_id=? AND raw_message_id=?",
        (user_id, data["raw_message_id"]),
    )
    values = _notification_machine_values(data)
    if existing:
        await execute(
            """UPDATE notification SET
                 title=?, summary=?, location=?, due_at=?, due_text=?, due_confidence=?,
                 evidence=?, conflict=?, candidates=?, extractor=?, model=?,
                 prompt_ver=?, updated_at=?
               WHERE id=? AND user_id=?""",
            (*values, now_ms(), existing["id"], user_id),
        )
        return existing["id"], False

    notif_id = new_id()
    ts = now_ms()
    await execute(
        """INSERT INTO notification
           (id, user_id, raw_message_id, group_id, group_name, sender_id, sender_name,
            source_ts, title, summary, location, due_at, due_text, due_confidence, evidence,
            conflict, candidates, extractor, model, prompt_ver, created_at, updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            notif_id,
            user_id,
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


async def get_notification_row(notif_id: str, user_id: str) -> dict | None:
    return await fetch_one(
        "SELECT * FROM notification WHERE id=? AND user_id=?", (notif_id, user_id)
    )


async def patch_notification(notif_id: str, user_id: str, fields: dict[str, Any]) -> bool:
    """改机器字段。`fields` 的键必须来自 NOTIFICATION_PATCHABLE（列名白名单）。"""
    unknown = [k for k in fields if k not in NOTIFICATION_PATCHABLE]
    if unknown:
        raise ValueError(f"不可修改的字段：{unknown}")
    if not fields:
        return await get_notification_row(notif_id, user_id) is not None
    sets = ", ".join(f"{k}=?" for k in fields)
    rowcount = await execute(
        f"UPDATE notification SET {sets}, updated_at=? WHERE id=? AND user_id=?",
        (*fields.values(), now_ms(), notif_id, user_id),
    )
    return rowcount > 0


async def touch_notification(notif_id: str, user_id: str) -> None:
    """把 updated_at 推到当前时间。

    人工修正与已读都会改变读投影，`since` 是"有没有变过"的游标，
    所以它们也要推一下 —— 否则前端增量同步会漏掉这些变化。
    """
    await execute(
        "UPDATE notification SET updated_at=? WHERE id=? AND user_id=?",
        (now_ms(), notif_id, user_id),
    )


async def delete_notification(notif_id: str, user_id: str) -> bool:
    """删除通知行。correction / read_state 保留（只追加层不做级联删除）。"""
    return (
        await execute(
            "DELETE FROM notification WHERE id=? AND user_id=?", (notif_id, user_id)
        )
        > 0
    )


async def count_notifications(user_id: str) -> int:
    return await count_of(
        "SELECT COUNT(*) AS c FROM notification WHERE user_id=?", (user_id,)
    )


# --------------------------------------------------------------------------
# 人工修正 / 已读
# --------------------------------------------------------------------------


async def add_correction(
    notif_id: str, user_id: str, field: str, value: Any, actor: str = "web"
) -> str:
    """追加一条人工修正。

    `user_id` 是**租户**（这条数据属于谁），`actor` 是**操作者**
    （界面上显示"谁改的"）。两个都要传，别混。
    """
    corr_id = new_id()
    await execute(
        """INSERT INTO correction (id, user_id, notification_id, field, value, actor, ts)
           VALUES (?,?,?,?,?,?,?)""",
        (
            corr_id,
            user_id,
            notif_id,
            field,
            None if value is None else str(value),
            actor,
            now_ms(),
        ),
    )
    await touch_notification(notif_id, user_id)
    return corr_id


async def list_corrections(notif_id: str, user_id: str) -> list[dict]:
    """修正历史，按时间正序（契约 §2）。"""
    return await fetch_all(
        """SELECT id, notification_id, field, value, actor, ts FROM correction
           WHERE notification_id=? AND user_id=? ORDER BY ts ASC, id ASC""",
        (notif_id, user_id),
    )


async def set_read(notif_id: str, user_id: str, read: bool) -> None:
    if read:
        await execute(
            "INSERT OR REPLACE INTO read_state (notification_id, user_id, read_at)"
            " VALUES (?,?,?)",
            (notif_id, user_id, now_ms()),
        )
    else:
        await execute(
            "DELETE FROM read_state WHERE notification_id=? AND user_id=?",
            (notif_id, user_id),
        )
    await touch_notification(notif_id, user_id)


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
    user_id: str,
    group_id: str,
    group_name: str | None,
    from_ts: int,
    to_ts: int,
    reason: str | None,
) -> str:
    alert_id = new_id("gap_")
    await execute(
        """INSERT INTO gap_alert
           (id, user_id, group_id, group_name, from_ts, to_ts, reason, created_at, acknowledged)
           VALUES (?,?,?,?,?,?,?,?,0)""",
        (
            alert_id,
            user_id,
            str(group_id),
            group_name,
            int(from_ts),
            int(to_ts),
            reason,
            now_ms(),
        ),
    )
    return alert_id


async def list_gap_alerts(
    user_id: str, *, acknowledged: bool | None = None, limit: int = 20
) -> list[dict]:
    where = " WHERE user_id=?"
    params: list[Any] = [user_id]
    if acknowledged is not None:
        where += " AND acknowledged=?"
        params.append(1 if acknowledged else 0)
    return await fetch_all(
        f"SELECT * FROM gap_alert{where} ORDER BY created_at DESC LIMIT ?",
        (*params, int(limit)),
    )


async def ack_gap_alert(alert_id: str, user_id: str) -> bool:
    return (
        await execute(
            "UPDATE gap_alert SET acknowledged=1 WHERE id=? AND user_id=?",
            (alert_id, user_id),
        )
        > 0
    )


# --------------------------------------------------------------------------
# 统计：后端只做累加
# --------------------------------------------------------------------------

STAT_FIELDS = ("ingested", "extracted", "unparsed", "conflicts", "degraded", "llm_tokens")


async def add_stats(user_id: str, day: str | None, fields: dict[str, int]) -> dict:
    """把 `fields` 里的计数累加到 `(user_id, day)` 那一行，返回累加后的整行。

    字段含义后端一概不理解：来的是已知列就加，未知键忽略（契约：未知字段忽略）。
    """
    d = (day or "").strip() or local_day()
    increments = {k: int(v) for k, v in fields.items() if k in STAT_FIELDS}
    await execute(
        "INSERT OR IGNORE INTO pipeline_stat (user_id, day) VALUES (?,?)", (user_id, d)
    )
    if increments:
        sets = ", ".join(f"{k} = {k} + ?" for k in increments)
        await execute(
            f"UPDATE pipeline_stat SET {sets} WHERE user_id=? AND day=?",
            (*increments.values(), user_id, d),
        )
    return await get_stat(user_id, d)


async def get_stat(user_id: str, day: str | None = None) -> dict:
    d = (day or "").strip() or local_day()
    row = await fetch_one(
        "SELECT * FROM pipeline_stat WHERE user_id=? AND day=?", (user_id, d)
    )
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
    user_id: str,
    *,
    day: str | None,
    kind: str,
    text: str,
    sent: bool,
    error: str | None,
) -> tuple[str, bool]:
    """写入一条发送记录，返回 (id, 是否新写入)。

    幂等键是 `(user_id, day, kind, sent)`：同一个用户同一天同一 kind 同一结果
    重复提交返回已有 id。这样 bot 重启/重试不会把"今天发过没有"的答案搞成 2 ——
    对收件人来说重发一条摘要比漏发更糟，所以这里的幂等是刻意的。
    """
    d = (day or "").strip() or local_day()
    sent_int = 1 if sent else 0
    log_id = new_id()
    rowcount = await execute(
        """INSERT INTO digest_log (id, user_id, day, kind, text, sent, error, ts)
           SELECT ?,?,?,?,?,?,?,?
           WHERE NOT EXISTS (
             SELECT 1 FROM digest_log WHERE user_id=? AND day=? AND kind=? AND sent=?
           )""",
        (
            log_id, user_id, d, kind, text, sent_int, error, now_ms(),
            user_id, d, kind, sent_int,
        ),
    )
    if rowcount == 0:
        existing = await fetch_one(
            """SELECT id FROM digest_log WHERE user_id=? AND day=? AND kind=? AND sent=?
               ORDER BY ts ASC, id ASC LIMIT 1""",
            (user_id, d, kind, sent_int),
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
    user_id: str,
    *,
    day: str | None = None,
    kind: str | None = None,
    sent: bool | None = None,
    limit: int = 50,
) -> list[dict]:
    where, params = _digest_log_filters(day, kind, sent)
    # user_id 放最前：它是**必需**条件，其余是可选过滤
    glue = " AND " if where else " WHERE "
    return await fetch_all(
        f"SELECT * FROM digest_log{where}{glue}user_id=? ORDER BY ts DESC, id DESC LIMIT ?",
        (*params, user_id, int(limit)),
    )


async def count_digest_logs(
    user_id: str,
    *,
    day: str | None = None,
    kind: str | None = None,
    sent: bool | None = None,
) -> int:
    where, params = _digest_log_filters(day, kind, sent)
    glue = " AND " if where else " WHERE "
    return await count_of(
        f"SELECT COUNT(*) AS c FROM digest_log{where}{glue}user_id=?",
        (*params, user_id),
    )


# --------------------------------------------------------------------------
# bot 的键值暂存（契约 §11）
#
# 过期判定**在读取时**做：物理清理只是省空间，正确性不建立"有个后台任务会删"上。
# --------------------------------------------------------------------------


def _state_expired(row: dict, now: int) -> bool:
    expires_at = row.get("expires_at")
    return expires_at is not None and int(expires_at) <= now


async def put_state(
    user_id: str, namespace: str, key: str, value_json: str, expires_at: int | None = None
) -> dict:
    stamp = now_ms()
    await execute(
        """INSERT INTO bot_state
             (user_id, namespace, "key", value, expires_at, created_at, updated_at)
           VALUES (?,?,?,?,?,?,?)
           ON CONFLICT(user_id, namespace, "key") DO UPDATE SET
             value=excluded.value,
             expires_at=excluded.expires_at,
             updated_at=excluded.updated_at""",
        (user_id, namespace, key, value_json, expires_at, stamp, stamp),
    )
    return {"namespace": namespace, "key": key, "expires_at": expires_at}


async def get_state(
    user_id: str, namespace: str, key: str, *, now: int | None = None
) -> dict | None:
    """取一个键；已过期视为不存在（并顺手删掉）。"""
    moment = now_ms() if now is None else now
    row = await fetch_one(
        'SELECT * FROM bot_state WHERE user_id=? AND namespace=? AND "key"=?',
        (user_id, namespace, key),
    )
    if row is None:
        return None
    if _state_expired(row, moment):
        await delete_state(user_id, namespace, key)
        return None
    return row


async def delete_state(user_id: str, namespace: str, key: str) -> bool:
    return (
        await execute(
            'DELETE FROM bot_state WHERE user_id=? AND namespace=? AND "key"=?',
            (user_id, namespace, key),
        )
        > 0
    )


async def list_state(
    user_id: str, namespace: str, *, limit: int | None = None, now: int | None = None
) -> list[dict]:
    """列出一个用户某个 namespace 下**未过期**的键值。"""
    moment = now_ms() if now is None else now
    sql = (
        "SELECT * FROM bot_state WHERE user_id=? AND namespace=?"
        " AND (expires_at IS NULL OR expires_at > ?)"
        ' ORDER BY updated_at DESC, "key" ASC'
    )
    params: list[Any] = [user_id, namespace, moment]
    if limit is not None:
        sql += " LIMIT ?"
        params.append(int(limit))
    rows = await fetch_all(sql, params)
    await purge_expired_state(namespace, user_id=user_id, now=moment)
    return rows


async def purge_expired_state(
    namespace: str | None = None, *, user_id: str | None = None, now: int | None = None
) -> int:
    """顺手清掉过期行 —— 只是省空间，读取路径不依赖它。

    `user_id=None` 时是**跨用户的全局清理**（运维用，不向任何人返回数据）。
    这是全项目唯一一处刻意不按用户过滤的**写**操作（读的那一处是
    `find_subscribers` 的投递名单），所以它走 `_execute_unscoped` 显式绕过
    护栏 —— 绕过得写出来，不能是疏忽。
    """
    moment = now_ms() if now is None else now
    clauses = ["expires_at IS NOT NULL", "expires_at <= ?"]
    params: list[Any] = [moment]
    if user_id is not None:
        clauses.insert(0, "user_id=?")
        params.insert(0, user_id)
    if namespace is not None:
        clauses.insert(0, "namespace=?")
        params.insert(0, namespace)
    return await _execute_unscoped(
        f"DELETE FROM bot_state WHERE {' AND '.join(clauses)}", tuple(params)
    )


# --------------------------------------------------------------------------
# 订阅（契约 §12）
#
# 这一层只做 SQL，不做好坏判断 —— "sender_id 不能为空、不能是通配符"之类
# 属于业务规则，放在 subscriptions.py，这样从 bot / 前端进来的写入都过同一道关。
# --------------------------------------------------------------------------


async def insert_subscription(
    user_id: str,
    group_id: str,
    sender_id: str,
    *,
    group_name: str | None = None,
    sender_name: str | None = None,
    note: str | None = None,
) -> tuple[dict, bool]:
    """写入（或重新启用）一条订阅。返回 `(行, 是否新建)`。

    同一个人对同一个 (群, 发送者) 再订一次**不报错**：这和"把一个关掉的订阅
    重新打开"是同一个意图，报 409 只会逼前端多做一次查询。

    名字和备注只在**非空**时覆盖：重新订阅时前端可能只填了 id，
    不该把之前记下的群名抹掉。
    """
    existing = await fetch_one(
        "SELECT * FROM subscription WHERE user_id=? AND group_id=? AND sender_id=?",
        (user_id, group_id, sender_id),
    )
    stamp = now_ms()
    clean = {
        "group_name": (group_name or "").strip() or None,
        "sender_name": (sender_name or "").strip() or None,
        "note": (note or "").strip() or None,
    }
    if existing:
        await execute(
            """UPDATE subscription SET
                 enabled=1,
                 group_name=COALESCE(?, group_name),
                 sender_name=COALESCE(?, sender_name),
                 note=COALESCE(?, note),
                 updated_at=?
               WHERE id=? AND user_id=?""",
            (clean["group_name"], clean["sender_name"], clean["note"], stamp, existing["id"], user_id),
        )
        row = await fetch_one(
            "SELECT * FROM subscription WHERE id=? AND user_id=?", (existing["id"], user_id)
        )
        return row or existing, False

    sub_id = new_id("sub_")
    await execute(
        """INSERT INTO subscription
             (id, user_id, group_id, sender_id, group_name, sender_name, note,
              enabled, created_at, updated_at)
           VALUES (?,?,?,?,?,?,?,1,?,?)""",
        (
            sub_id,
            user_id,
            group_id,
            sender_id,
            clean["group_name"],
            clean["sender_name"],
            clean["note"],
            stamp,
            stamp,
        ),
    )
    row = await fetch_one("SELECT * FROM subscription WHERE id=? AND user_id=?", (sub_id, user_id))
    return row or {"id": sub_id, "user_id": user_id, "group_id": group_id, "sender_id": sender_id}, True


async def list_subscriptions(
    user_id: str, *, include_disabled: bool = True, limit: int | None = None
) -> list[dict]:
    sql = "SELECT * FROM subscription WHERE user_id=?"
    params: list[Any] = [user_id]
    if not include_disabled:
        sql += " AND enabled=1"
    sql += " ORDER BY updated_at DESC, id DESC"
    if limit is not None:
        sql += " LIMIT ?"
        params.append(int(limit))
    return await fetch_all(sql, params)


async def get_subscription(user_id: str, sub_id: str) -> dict | None:
    return await fetch_one(
        "SELECT * FROM subscription WHERE id=? AND user_id=?", (sub_id, user_id)
    )


async def update_subscription(
    user_id: str, sub_id: str, fields: dict
) -> dict | None:
    """只改传进来的字段。字段名由调用方（subscriptions.py）白名单过滤过。"""
    allowed = ("enabled", "note", "group_name", "sender_name")
    sets = [f"{name}=?" for name in allowed if name in fields]
    if not sets:
        return await get_subscription(user_id, sub_id)
    params: list[Any] = [fields[name] for name in allowed if name in fields]
    params.extend([now_ms(), sub_id, user_id])
    changed = await execute(
        f"UPDATE subscription SET {', '.join(sets)}, updated_at=? WHERE id=? AND user_id=?",
        tuple(params),
    )
    if changed == 0:
        return None
    return await get_subscription(user_id, sub_id)


async def delete_subscription(user_id: str, sub_id: str) -> bool:
    return (
        await execute(
            "DELETE FROM subscription WHERE id=? AND user_id=?", (sub_id, user_id)
        )
        > 0
    )


async def count_subscriptions(user_id: str, *, enabled_only: bool = False) -> int:
    sql = "SELECT COUNT(*) AS c FROM subscription WHERE user_id=?"
    if enabled_only:
        sql += " AND enabled=1"
    return await count_of(sql, (user_id,))


async def find_subscribers(group_id: str, sender_id: str | None = None) -> list[str]:
    """**投递名单**：这条 (群, 发送者) 的消息，哪些用户要？

    这是全项目唯一一处刻意跨用户读的用户表查询（理由见 `_fetch_all_unscoped`）。
    只返回 user_id，不返回订阅行的其他内容 —— bot 只需要知道"扇给谁"，
    每个用户各自的 group_name / note 是他们的私事。

    `sender_id=None` 表示"这个群里**任何**发送者" —— 缺口告警用它：
    缺口是群级事件（"这个群中间断了一段"），凡是订了这个群里任何人的用户都该知道，
    而投递名单本身仍然是按 (群, 发送者) 的，两种语义不要混。

    服务端去重（DISTINCT）而不是让 bot 去重：唯一约束在这里是
    (user_id, group_id, sender_id)，理论上一个人对同一个发送者只会有一行，
    但同一个人可以订同一个群里的多个人 —— 缺口告警必须只通知他一次。
    """
    clauses = ["group_id=?", "enabled=1"]
    params: list[Any] = [str(group_id)]
    if sender_id is not None:
        clauses.append("sender_id=?")
        params.append(str(sender_id))
    rows = await _fetch_all_unscoped(
        f"SELECT DISTINCT user_id FROM subscription WHERE {' AND '.join(clauses)}"
        " ORDER BY user_id",
        tuple(params),
    )
    return [str(r["user_id"]) for r in rows if r.get("user_id")]

