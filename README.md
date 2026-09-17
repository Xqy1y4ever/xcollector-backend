# Xcollector Backend

**本服务是纯数据层**：只做增删查改（SQLite + HTTP），不做任何业务判断。

「什么是通知」「该不该抽」「DDL 对不对」「今天盲区多少」全部由
[`xcollector-bot`](../xcollector-bot/) 决定；后端只负责把它写下来、再读回去。
唯一的例外是 `GET /api/notifications` 的**读投影**（把人工修正覆盖到通知上、
由 `due_at` 推导 `status`）—— 前端直接消费后端，必须拿到这个视图。

> **接口契约见 [`docs/api.md`](docs/api.md)，那是三个仓库之间唯一的约定。**
> 本文件只讲怎么跑起来、配置有哪些、以及边界的理由。

Python **3.12+**。完整设计背景见 [`docs/design.md`](docs/design.md)。

---

## 职责边界

| 属于**后端** | 属于 **bot** |
|---|---|
| 存储（SQLite）与表结构 | 连 OneBot / NapCat |
| 增删查改接口 | 群与发送者白名单筛选 |
| **读投影**：`correction` 覆盖 `notification`、由 `due_at` 推导 `status` | 抽取（规则 + 大模型 + 交叉验证） |
| 附件二进制存取 | 合并转发展开、每条消息一行日志 |
| 唯一性约束与幂等 | 缺口检测、每日摘要组装与发送 |
| 存储健康检查 | 私聊指令、统计计数 |

**后端不认识的词**：QQ、OneBot、群白名单、大模型、抽取、digest、盲区、重试、附件下载。
一旦这些词出现在后端代码里，就是越界了（`tests/check_config.py` 与人工 grep 都盯这条）。

```
QQ/NapCat ──OneBot WS──▶ xcollector-bot ──HTTP /api/*──▶ xcollector-backend
                              │                                  ▲
                              └── 前端只跟后端打交道：xcollector-web ─┘
```

---

## 快速开始

```bash
# 1. 建虚拟环境并装依赖
python -m venv .venv
.venv\Scripts\activate            # Windows
pip install -r requirements.txt

# 2. 复制配置（只有 9 项，全部有默认值；本地开发可以先不改）
copy .env.example .env

# 3. 起服务
python -m app.main
#    交互式文档：http://127.0.0.1:8000/docs
#    bot：另开一个终端跑 ../xcollector-bot（不跑就没有新数据进来）
```

没有演示数据、没有后台任务、没有对外连接：**启动只做两件事** —— 打开 SQLite
（含增量迁移）、检查共享密钥有没有配。数据由 bot 推、前端由 bot 触发。

---

## 接口清单

前缀 `/api`，全部端点都在 [`docs/api.md`](docs/api.md) 里有请求/响应示例。

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/messages` | 创建原始消息（幂等：`(group_id, message_id)`） |
| GET | `/api/messages` | 列表：`state`（重复或逗号分隔）/ `group_id` / `since` / `limit` / `count_only=1` |
| GET | `/api/messages/{id}` | 单条（含 `content` / `attachments` / `raw` / `state`） |
| PATCH | `/api/messages/{id}` | 只改 `state` / `state_reason` / `attachments`，其余字段一律忽略 |
| POST | `/api/notifications` | 创建或更新（幂等：`raw_message_id`）；`evidence` 为空 → **400** |
| GET | `/api/notifications` | 列表（**读投影**）：`since` / `status` / `q` / `limit` / `count_only=1` |
| GET | `/api/notifications/{id}` | `{notification, raw}` |
| PATCH | `/api/notifications/{id}` | 改机器字段；`status` / `read` 会被**忽略** |
| DELETE | `/api/notifications/{id}` | 删除 |
| POST | `/api/notifications/{id}/corrections` | 人工修正（只追加） |
| GET | `/api/notifications/{id}/corrections` | 修正历史（时间正序） |
| POST | `/api/notifications/{id}/read` | 已读 / 未读 |
| POST | `/api/attachments` | `multipart/form-data` 上传，超 `MEDIA_MAX_BYTES` → **413** |
| GET | `/api/attachments/{id}` | 二进制 + 正确的 `Content-Type` / `Content-Disposition` |
| POST | `/api/groups` | upsert 群状态，返回 **`previous_last_msg_ts`** |
| GET | `/api/groups` | 群状态列表 |
| POST | `/api/gap-alerts` | 记录缺口告警 |
| GET | `/api/gap-alerts` | `acknowledged` / `limit` 过滤 |
| POST | `/api/gap-alerts/{id}/ack` | 确认 |
| POST | `/api/stats` | 计数累加（后端不理解字段含义） |
| GET | `/api/stats` | `day`（省略用服务器当天） |
| POST | `/api/digest-log` | digest 发送记录（幂等：`(day, kind, sent)`） |
| GET | `/api/digest-log` | `day` / `kind` / `sent` / `limit` / `count_only=1` |
| PUT | `/api/state/{namespace}/{key}` | bot 的键值暂存（幂等 upsert，可带 `ttl_seconds`） |
| GET | `/api/state/{namespace}/{key}` | 取值；**过期或不存在一律 404** |
| DELETE | `/api/state/{namespace}/{key}` | 删除（幂等） |
| GET | `/api/state/{namespace}` | 列出未过期的键值 / `count_only=1` |
| GET | `/api/health` | **只报存储自身**（不报 QQ / 大模型 / 流水线） |

### 关键约定（实现时守住的东西）

- **认证**：所有 `/api` 请求带 `Authorization: Bearer <令牌>`；两个令牌都为空
  则不校验（仅本地开发）并打 WARNING。**两个令牌，两个范围**：
  `API_TOKEN` 是写入令牌（只有 bot 有），`WEB_API_TOKEN` 是网页令牌
  （只能读 + 人工修正 + 标已读）。范围不够返回 **403**，令牌不对返回 **401**。
  `WEB_API_TOKEN` 留空 = 退回单令牌模式（行为与拆分前一致，但没有分级）。
  细节见 [`docs/api.md`](docs/api.md) 的「通用约定」。
- **附件下载是唯一允许不带 Authorization 头的接口**：浏览器 `<img>` 带不了那个头，
  所以读投影里的附件 `url` 是每次读取现签的短时效签名链接（见 `app/signing.py`）。
- **幂等**：`POST /api/messages` 靠 `(group_id, message_id)`，`POST /api/notifications`
  靠 `raw_message_id`，`POST /api/digest-log` 靠 `(day, kind, sent)`；重复提交返回已有 id。
- **证据硬约束**：`evidence` 为空 → 400「没有证据的条目不许入库」。后端替 bot 守住这条。
- **留痕字段不可直改**：`status` / `read` 只能走 `corrections` 与 `/read`，`PATCH` 传了会被忽略。
- **原始层只追加**：`content` / `raw` / `ts` / `message_id` / `group_id` / `sender_id`
  写进去就不再改；`attachment` 的元数据可以事后补齐（附件是"后补"，不是"改写"）。
- **`count_only=1`**：每个列表接口都支持，只返回 `{"count": n}`，让 bot 不必拉全表。
- **`since` 是「有没有变过」的游标**：通知的 `updated_at` 在机器字段被改、人工修正写入、
  已读状态变化时都会推进，所以增量同步不会漏掉"只是一次人工修正"这种变化。
- **读投影在 SQL 层**：列表接口用**一次 JOIN + 相关子查询**取最新修正与已读，
  没有任何 N+1；`since` / `status` / `q` 也在同一层 SQL 里过滤，`limit` 语义才是对的。
- **时间戳一律毫秒整数**。

---

## 配置（只剩这些）

全部 12 项，见 [`.env.example`](.env.example)。

| 配置 | 默认 | 说明 |
|---|---|---|
| `API_TOKEN` | 空 | **写入令牌**（bot 侧同名）。只有 bot 有，不要给浏览器。空 = 不校验，仅本地开发 |
| `WEB_API_TOKEN` | 空 | **网页令牌**（bot 侧同名）。只能读 + 人工修正 + 标已读。留空 = 退回单令牌模式 |
| `ATTACHMENT_URL_TTL` | `3600` | 附件签名 URL 有效期（秒）。0 = 不签名（`<img>` 会 401） |
| `ATTACHMENT_SIGN_KEY` | 空 | 附件签名密钥。留空 = 从 `API_TOKEN` 派生 |
| `DB_PATH` | `data/xcollector.db` | SQLite 路径 |
| `ATTACHMENT_DIR` | `data/attachments` | 附件二进制目录 |
| `MEDIA_MAX_BYTES` | `5242880` | 单个附件上限，超限 413 |
| `SERVER_HOST` | `127.0.0.1` | 监听地址 |
| `SERVER_PORT` | `8000` | 监听端口 |
| `CORS_ORIGINS` | 本地 5173 | 允许的前端来源，逗号分隔 |
| `LOG_LEVEL` | `INFO` | 日志级别 |
| `LOG_PREVIEW_CHARS` | `60` | 日志里文本预览长度 |

**配置按进程划分，不按功能划分。** 判据是：这个值改变时，需要重启哪个进程？
凡是跟 QQ 接入、白名单、抽取、digest 有关的一律在 bot 那边配 ——
在这里放一个后端永远不读的配置项，只会让人改了之后困惑为什么没生效。
`tests/check_config.py` 会把这条规则当作可执行的检查来跑。

---

## 数据模型与分层铁律

| 表 | 可变性 | 说明 |
|---|---|---|
| `raw_message` | **只追加** | 唯一不可再生的资产。`content`/`raw` 永不修改，只有 `state*` 三列会更新 |
| `notification` | 可整表重建 | 派生层。bot 改 prompt / 换模型后可以整表重跑 |
| `correction` | **只追加** | 人工修正。展示时覆盖 `notification`，所以**重跑永远不会冲掉你改过的 DDL** |
| `read_state` | 可覆盖 | 已读状态 |
| `attachment` | 只追加 | 附件元数据；二进制在 `ATTACHMENT_DIR`，文件名由 id 生成 |
| `group_state` | 可覆盖 | 群最后消息时间与今日计数（缺口检测要用"上一条的时间"） |
| `gap_alert` | 可追加/确认 | 缺口告警 |
| `pipeline_stat` | 累加 | 计数，后端不理解每个字段的含义 |
| `digest_log` | 幂等追加 | 发送记录，后端不校验 `kind` 取值 |
| `bot_state` | 可覆盖 | bot 的键值暂存（带 TTL）；**过期在读取时判定** |

新增列走 `app/db.py` 的 `_MIGRATIONS`（SQLite 没有 `ADD COLUMN IF NOT EXISTS`）；
新增表由 `SCHEMA` 里的 `CREATE TABLE IF NOT EXISTS` 自动补到老库上。

---

## 自检

```bash
# 配置边界（字段都有引用 / .env.example 无错键 / 两边不越界 / 共享密钥同名）
python -m tests.check_config

# 人工修正不被重跑覆盖（自造数据，跑完复原）
python -m tests.check_corrections

# HTTP 端到端冒烟（需要另开一个终端跑着后端）
#   终端 1（先确认 8001 空闲：netstat -ano | findstr :8001）
#   $env:API_TOKEN='smoke-token'; $env:DB_PATH='data/smoke.db'
#   $env:ATTACHMENT_DIR='data/smoke-att'; $env:MEDIA_MAX_BYTES='1048576'
#   $env:SERVER_PORT='8001'; .\.venv\Scripts\python.exe -m app.main
#   终端 2
#   $env:SMOKE_BASE='http://127.0.0.1:8001'; $env:SMOKE_TOKEN='smoke-token'
#   $env:SMOKE_MEDIA_MAX='1048576'; .\.venv\Scripts\python.exe -m tests.check_api
python -m tests.check_api

# 语法检查
python -m compileall -q app tests
```

---

## 目录结构

```
app/
  config.py         配置（9 项，全部有默认值）
  db.py             schema、增量迁移、CRUD（唯一碰 SQL 的地方之一）
  materialize.py    读投影：correction 覆盖 notification + status 推导 + 行形状
  attachments.py    multipart 解析、落盘、防目录穿越、响应头
  utils.py          ID / 时间 / 日志预览
  logging_setup.py  日志初始化（入口脚本共用）
  api/routes.py     HTTP 接口 + Bearer 认证
  main.py           FastAPI 入口（只做 init_db + 打日志）
docs/api.md         三个仓库之间的唯一契约
tests/              check_api / check_config / check_corrections
data/               SQLite 与附件（不进版本库）
```

---

## 已知边界（诚实清单）

1. **不适合多进程 / 多 worker 部署**：SQLite + 单写入进程。要横向扩就换 Postgres。
2. **不做业务校验**：后端不判断 `state` / `kind` / `namespace` 的取值是否合理，
   也不判断 `due_at` 是不是"合理的截止时间" —— 那是 bot 的职责。
   例外只有契约写死的那几条（`evidence` 非空、`status` 三种取值、可修正字段集合）。
3. **`attachment` 表不做引用计数**：删除通知不会删附件，附件也不会自动过期清理。
   需要清理时手工按 `created_at` 处理。
4. **`correction` / `read_state` 不做级联删除**：删除通知时保留修正历史（只追加层）。
5. **`digest_log` 的幂等键是 `(day, kind, sent)`**：同一天同一 kind 同一结果只留一行。
   这是刻意的 —— bot 重启后不该把当天摘要重发一遍。
6. **附件内容按上传时声明的类型返回**，因此对非图片/音视频/PDF 一律
   用 `Content-Disposition: attachment` + `nosniff` 下载，避免被当成页面执行。
