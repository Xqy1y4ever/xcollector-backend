# Xcollector 后端 CRUD 契约

> **这份契约是三个仓库之间唯一的约定。** 后端的定位是**纯数据层**：
> 只做增删查改，不做任何业务判断。所有"什么是通知""该不该抽""DDL 对不对"
> 都在 `xcollector-bot` 里决定，后端只是把它写下来、再读回去。

## 职责边界（改代码前先读这一段）

| 属于**后端** | 属于 **bot** |
|---|---|
| 存储（SQLite）与表结构 | 连 OneBot / NapCat |
| 增删查改接口 | 群与发送者白名单筛选 |
| **读投影**：把 `correction` 覆盖到 `notification` 上、由 `due_at` 推导 `status` | 抽取（规则 + LLM + 交叉验证） |
| 附件二进制存取 | 合并转发展开、每条消息一行日志 |
| 唯一性约束与幂等 | 缺口检测、每日 digest 组装与发送 |
| 存储健康检查 | 私聊指令、统计计数 |
| **一切需要跨重启存活的状态**：digest 发送记录、指令的待确认、`/list` 编号映射 | **只持有可随时丢弃的内存**：连接对象、群名缓存 |

**后端不认识的词**：QQ、OneBot、群白名单、LLM、抽取、digest、盲区。
一旦这些词出现在后端代码里，就是越界了。

> 唯一的例外是**读投影**（见 `GET /api/notifications`）：前端直接消费后端，
> 它必须拿到"人工修正已生效、status 已推导"的视图。这是「查」，不是业务逻辑。

## 通用约定

- 前缀 `/api`
- 认证：所有请求带 `Authorization: Bearer <API_TOKEN>`；`API_TOKEN` 为空则不校验（仅本地开发）
  - **整套系统只有一个共享密钥**，两边都叫 `API_TOKEN`。后端不再主动调用 bot，
    所以不再需要第二个令牌。
- 时间戳一律**毫秒整数**（如 `1757692800000`）
- 所有写接口**幂等**：重复提交不会产生重复行
- 未知字段忽略，不报错
- 出错返回 `{"detail": "..."}`，HTTP 4xx/5xx

---

## 1. 原始消息 `raw_message`

原始消息是**只追加**的：`content`/`raw` 一旦写入永不修改，只有 `state` 三个字段可变。

### `POST /api/messages` — 创建（幂等）

```json
{
  "message_id": "12345",
  "group_id": "123456789",
  "group_name": "示例通知群",
  "sender_id": "10001",
  "sender_name": "张老师",
  "ts": 1757692800000,
  "content": "@全体成员 大家下周三前把军训心得交到班长那里",
  "attachments": [
    {"id": "att_xxx", "type": "image", "name": "x.jpg", "size": 12345, "url": "/api/attachments/att_xxx"}
  ],
  "raw": {}
}
```

响应：

```json
{"id": "01J8XK2M9P", "is_new": true}
```

同一 `(group_id, message_id)` 已存在时返回已有 `id` 且 `is_new: false`。

### `GET /api/messages/{id}` → 行（含 `content`、`attachments`、`raw`、`state`）

### `GET /api/messages`

Query：`state`（可重复或逗号分隔）、`group_id`、`since`（ts 毫秒）、`limit`（默认 100，上限 1000）、`count_only=1`

`count_only=1` 时只返回 `{"count": 42}`，不返回行。

### `PATCH /api/messages/{id}`

**可改字段只有两个**：`state` / `state_reason`，以及 `attachments`。

```json
{"state": "extracted", "state_reason": null, "attachments": [{"id": "att_xxx", "type": "image", "url": "/api/attachments/att_xxx"}]}
```

`state` 取值由 **bot** 定义，后端不校验取值，只存字符串。

**为什么允许改 `attachments`**：原始消息是唯一不可再生的资产，所以 bot 必须先把它
落库（写前日志），之后才去下载附件、上传、回填。附件因此是"事后补齐"，不是"修改"。

`content` / `raw` / `ts` / `message_id` / `group_id` / `sender_id` 等**一律不可改** ——
那是消息本体，改了就破坏了"原始层只追加"这条铁律。传了要忽略。

响应返回更新后的行。

---

## 2. 通知 `notification`

### `POST /api/notifications` — 创建或更新（按 `raw_message_id` 幂等）

```json
{
  "raw_message_id": "01J8XK2M9P",
  "group_id": "123456789",
  "group_name": "示例通知群",
  "sender_id": "10001",
  "sender_name": "张老师",
  "source_ts": 1757692800000,
  "title": "提交军训心得",
  "summary": "全体大一需提交不少于 800 字",
  "location": "教三201",
  "due_at": 1758124740000,
  "due_text": "下周三前",
  "due_confidence": 0.72,
  "evidence": "@全体成员 大家下周三前把军训心得交到班长那里",
  "conflict": false,
  "candidates": [{"model": "deepseek/deepseek-chat", "due_at": 1758124740000, "due_text": "下周三前"}],
  "extractor": "llm",
  "model": "deepseek/deepseek-chat",
  "prompt_ver": "llm-v2"
}
```

响应：`{"id": "01J...", "created": true}`

`evidence` 为空字符串时**返回 400**：后端替 bot 守住这条硬约束（没有证据的条目不许入库）。

### `GET /api/notifications` — 列表（**读投影**）

Query：
- `since`：只返回 `updated_at > since` 的行（增量同步）
- `status`：`all`（默认）/ `active` / `expired` / `done` / `archived`
- `q`：在 `title` / `summary` / `evidence` 上做子串匹配
- `limit`（默认 500，上限 2000）
- `count_only=1` → `{"count": n}`

响应：

```json
{
  "server_time": 1757692800000,
  "notifications": [ { ...读投影对象... } ]
}
```

**读投影对象**的字段（这是前端真正消费的形状）：

| 字段 | 说明 |
|---|---|
| `id` | 通知 id |
| `group_id` / `group_name` | 来源 |
| `sender_id` / `sender_name` | 发布者 |
| `title` / `summary` / `location` | 可由人工修正覆盖 |
| `due_at` / `due_text` / `due_confidence` | 可由人工修正覆盖 |
| `conflict` / `candidates` | 原样透出 |
| `evidence` | 原文依据 |
| `status` | 人工修正优先；否则 `due_at` 已过 → `expired`，其余 `active` |
| `manually_edited` | 是否有任何人工修正 |
| `read` | 是否已读 |
| `attachments` | 从对应的 `raw_message` 取 |
| `extractor` / `model` / `prompt_ver` | 溯源 |
| `source_ts` / `created_at` / `updated_at` | 时间 |

### `GET /api/notifications/{id}`

```json
{"notification": { ...读投影对象... }, "raw": { ...raw_message 行... }}
```

### `PATCH /api/notifications/{id}` — 直接改机器字段

bot 重跑抽取时用。可改：`title` / `summary` / `location` / `due_at` / `due_text` /
`due_confidence` / `evidence` / `conflict` / `candidates` / `model` / `prompt_ver`。

**不允许改 `status` 和 `read`** —— 那两个只能走 `corrections` 和 `/read`
（它们要留痕）。传了会被忽略。

响应返回更新后的读投影对象。

### `DELETE /api/notifications/{id}` → `{"deleted": true}`

### `POST /api/notifications/{id}/corrections` — 人工修正（只追加）

```json
{"field": "due_at", "value": 1758124740000, "user_id": "web"}
```

`field` 只能是 `title` / `summary` / `location` / `due_at` / `due_text` / `status`。
`status` 的值只能是 `active` / `archived` / `done`。

响应：`{"ok": true, "notification": { ...读投影对象... }}`

### `GET /api/notifications/{id}/corrections` → 修正历史（按时间正序）

### `POST /api/notifications/{id}/read` → `{"read": true}` / `{"read": false}`

---

## 3. 附件 `attachment`

**bot 下载字节后上传给后端**，后端负责存储与对外提供。这样前端只跟后端打交道。

### `POST /api/attachments` — `multipart/form-data`

| 字段 | 说明 |
|---|---|
| `file` | 二进制内容 |
| `filename` | 可选，原始文件名 |
| `source_url` | 可选，QQ CDN 的原始 URL（留档用） |

响应：

```json
{"id": "att_xxx", "url": "/api/attachments/att_xxx", "size": 12345, "content_type": "image/jpeg"}
```

上限 `MEDIA_MAX_BYTES`（默认 5MB），超限返回 413。

### `GET /api/attachments/{id}` → 二进制，带正确的 `Content-Type` 与 `Content-Disposition`

---

## 4. 群状态 `group_state`

bot 每收到一条消息就 upsert 一次。**缺口检测需要"上一条消息的时间"**，
所以 upsert 会把更新前的值一并返回，省掉一次竞态的读。

### `POST /api/groups` — upsert

```json
{"group_id": "123456789", "group_name": "示例通知群", "last_msg_ts": 1757692800000}
```

响应：

```json
{
  "group": {"group_id": "123456789", "group_name": "...", "last_msg_ts": 1757692800000, "msg_count_today": 12, "count_date": "2026-09-16"},
  "previous_last_msg_ts": 1757606400000
}
```

`previous_last_msg_ts` 为 `null` 表示这是第一次见到该群。

### `GET /api/groups` → `{"groups": [...]}`

---

## 5. 缺口告警 `gap_alert`

### `POST /api/gap-alerts`

```json
{"group_id": "123456789", "group_name": "...", "from_ts": 1757000000000, "to_ts": 1757060000000, "reason": "两条消息间隔 16.7 小时"}
```

响应：`{"id": "gap_xxx"}`

### `GET /api/gap-alerts?acknowledged=false&limit=20` → `{"alerts": [...]}`

### `POST /api/gap-alerts/{id}/ack` → `{"acknowledged": true}`

---

## 6. 流水线统计 `pipeline_stat`

bot 自己数，数完写进来。后端只做累加，不理解每个字段是什么意思。

### `POST /api/stats`

```json
{"day": "2026-09-16", "fields": {"ingested": 40, "extracted": 5, "unparsed": 2, "conflicts": 1, "degraded": 0, "llm_tokens": 18342}}
```

`day` 省略则用服务器当天。响应返回累加后的整行。

### `GET /api/stats?day=2026-09-16` → `{"day": "...", "ingested": 40, ...}`

---

## 7. 健康

### `GET /api/health`

**只报存储自身**，不报 OneBot / LLM / 流水线（那些是 bot 的事）。

```json
{
  "ok": true,
  "server_time": 1757692800000,
  "storage": {"driver": "sqlite", "path": "data/xcollector.db", "writable": true},
  "counts": {"messages": 1234, "notifications": 87, "attachments": 12},
  "version": "0.2.0"
}
```

---

## 8. 后端**不提供**的接口

以下全部由 bot 自己算、或由前端直接读上面这些接口拼出来：

| 曾经的接口 | 现在归谁 |
|---|---|
| `POST /api/ingest/messages` | bot 内部：筛选 → 抽取 → `POST /api/messages` + `POST /api/notifications` |
| `POST /api/tasks/manual` | bot 内部：解析 → 同样两个 POST |
| `GET /api/digest/preview`、`POST /api/digest/send` | bot：读 `/api/notifications` → 组装文本 → 自己发 |
| `blindspots`（在 `/api/notifications` 响应里） | bot：用 `count_only=1` 查几次自己算 |
| `GET /api/config/meta` | bot：把自己的运行时配置暴露在自己的 `/api/status` 上 |
| 群/发送者白名单 | bot 的配置，跟后端无关 |
| `EXTRACTOR` / `LLM_*` / `VLM_*` | bot 的配置 |

---

## 9. bot 暴露给前端的运行时状态

前端的「系统状态」页需要 OneBot 连接、今日流水线计数、盲区、缺口——这些现在
**全在 bot 手里**（后端只剩存储）。所以前端要调两个服务：

| 页面 | 调谁 |
|---|---|
| 通知台 `/` | **后端** `/api/notifications` |
| 系统状态 `/health` | **bot** `/api/status` |

前端通过 `vite.config.js` 的两个代理区分：`/api` → 后端，`/bot` → bot。

### `GET {BOT}/api/status` （在原有基础上扩展）

```json
{
  "onebot": {
    "connected": true,
    "mode": "client",
    "target": "ws://127.0.0.1:3001",
    "last_event_at": 1757692790000,
    "reconnect_count": 0,
    "last_error": null
  },
  "llm": {
    "extractor": "llm",
    "primary_model": "deepseek/deepseek-chat",
    "secondary_model": null,
    "cross_check_enabled": false,
    "vlm_enabled": false
  },
  "whitelist": {
    "groups": [{"group_id": "123456789", "name": null}],
    "senders": [{"sender_id": "10001", "name": "张老师"}],
    "sender_mode": "off"
  },
  "pipeline": {
    "today_ingested": 40,
    "today_extracted": 5,
    "today_unparsed": 2,
    "today_conflicts": 1,
    "today_degraded": 0,
    "today_llm_tokens": 18342
  },
  "blindspots": {
    "unparsed_count": 3,
    "conflict_count": 1,
    "low_confidence_count": 2,
    "degraded_today": false,
    "window_days": 7
  },
  "groups": [
    {"group_id": "123456789", "group_name": "示例通知群", "in_whitelist": true,
     "last_msg_ts": 1757692790000, "last_msg_at": 1757692790000,
     "silent_hours": 0.4, "msg_count_today": 12}
  ],
  "gap_alerts": [
    {"id": "gap_xxx", "group_id": "123456789", "group_name": "...",
     "from_ts": 1757000000000, "to_ts": 1757060000000, "reason": "...", "created_at": 1757060000000}
  ],
  "backend": {"reachable": true, "base_url": "http://127.0.0.1:8000"},
  "digest": {"enabled": true, "time": "21:30", "target_qq": "10001", "sent_today": false},
  "day": "2026-09-16",
  "server_time": 1757692800000
}
```

盲区计数、群列表、缺口告警都由 bot **现算**（用后端的 `count_only=1` 与列表接口），
所以字段名保持不变，前端改动量很小。

### `GET {BOT}/api/digest/preview` → `{"text": "..."}`

### `POST {BOT}/api/digest/send` `{"dry_run": true}` → `{"ok": true, "sent": false, "text": "...", "error": null}`

### 附件 URL

后端返回的 `attachments[].url` 形如 `/api/attachments/att_xxx`。
前端的 `/api` 已经代理到后端，所以**直接用这个相对路径即可**，不需要拼 base。
原来的 `local_path` 字段已废弃。

---

## 10. digest 发送记录 `digest_log`

bot 用它判断「今天的摘要发过没有」。

**为什么放后端**：bot 不允许持有任何需要跨重启存活的状态。
如果这个标记只在内存里，重启一次 bot 就会把当天的 digest 重发一遍 ——
对收件人来说就是"骚扰"，比漏发更糟。

### `POST /api/digest-log`

```json
{"day": "2026-09-16", "kind": "auto", "text": "【Xcollector 每日通知】...", "sent": true, "error": null}
```

`kind` 取值由 bot 定义（`auto` / `manual` / `preview`），后端只存字符串不校验。
`day` 省略则用服务器当天。响应：`{"id": "..."}`

### `GET /api/digest-log`

Query：`day`、`kind`、`sent`（`true`/`false`）、`limit`（默认 50）、`count_only=1`

→ `{"logs": [...]}` 或 `{"count": n}`

按 `ts` 倒序。`count_only=1` 让 bot 能只问「今天 auto 且 sent=true 的有几条」，
不用把正文全拉回来。

---

## 11. bot 的键值暂存 `bot_state`

指令的「待确认」（`/add` 解析没把握时回问，等用户回 y/n）和 `/list` 的
「编号 → 通知 id」映射，都需要跨重启存活：用户回复 `y` 时如果 bot 刚重启过，
那条待确认不该凭空消失。

**这里刻意做成不透明的键值对**：后端不理解 `value` 的含义，只负责存取和过期清理。
后端因此不需要知道「待确认」是什么东西 —— 它只是一块带 TTL 的持久化草稿纸。

### `PUT /api/state/{namespace}/{key}` —— 幂等 upsert

```json
{"value": {"text": "明天下午3点 交实验报告", "preview": {"title": "交实验报告"}}, "ttl_seconds": 600}
```

响应：`{"ok": true, "expires_at": 1757693400000}`

`value` 是任意 JSON（对象/数组/字符串/数字都行），后端原样存原样取。
`ttl_seconds` 省略表示不过期。

### `GET /api/state/{namespace}/{key}`

→ `{"key": "...", "value": ..., "expires_at": 1757693400000}`
已过期或不存在 → **404**

### `DELETE /api/state/{namespace}/{key}` → `{"deleted": true}`

### `GET /api/state/{namespace}` → `{"items": [{"key": "...", "value": ..., "expires_at": ...}]}`

**过期语义**：读取时 `expires_at` 已过的行一律视为不存在（返回 404 / 不出现在列表里）。
后端可以顺手删掉它们，但**读取时判定过期是必须的** —— 清理只是省空间，不是正确性依赖。

`namespace` 建议取值：`command_pending`（待确认）、`command_list`（/list 编号映射）、
`cache`（可丢弃的缓存，如群名）。


