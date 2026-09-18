# Xcollector Backend

Xcollector 的**数据层**：把 QQ 群里收到的官方通知存下来，并提供一套 HTTP 接口给
bot 写入、给每个用户读取自己的那一份。

它不做任何业务判断 —— 「什么是通知」「截止时间对不对」由
[`xcollector-bot`](https://github.com/Xqy1y4ever/xcollector-bot) 决定。后端只负责存、查、
和把**人工修正过的视图**读回去。

> **多用户服务**：每个用户在 QQ 里给机器人发 `/注册` 拿验证码，到网页上注册，
> 拿到一个只属于他自己的 `UserToken`。他订阅哪些「某个群里**某个人**说的话」
> 由他自己配；bot 处理所有订阅的**并集**，抽一次，再把结果扇给每个订阅者。
> 数据按 `user_id` 隔离，而且这条规则有自检脚本守着
> （`tests/check_scoping_guard.py`：任何碰了用户表的查询没有 `user_id` 就直接抛异常）。

> **项目主页与部署入口在
> [`xcollector-deploy`](https://github.com/Xqy1y4ever/xcollector-deploy)**
> —— 想看这套系统整体怎么跑、怎么装，从那里开始。

> 接口契约见 [`docs/api.md`](docs/api.md)，那是几个仓库之间唯一的约定。
> 设计背景见 [`docs/design.md`](docs/design.md)。

## 技术栈

| | |
|---|---|
| 语言 | Python 3.12+ |
| Web | FastAPI + Uvicorn |
| 存储 | SQLite（单文件，无外部依赖） |
| 附件 | 本地目录，库里只存元数据 |

没有 Redis、没有消息队列、没有后台任务：启动只做两件事 —— 打开数据库、检查令牌配置。

## 架构

```
QQ / NapCat ──OneBot──▶ xcollector-bot ──┐
                                         │  HTTP /api/*
nt_msg_export.db ──▶ xcollector-client ──┼──▶ xcollector-backend
                                         │            ▲
                        xcollector-web ──┴────────────┘
                        （前端只跟后端打交道）
```

| 属于后端 | 属于入库方（bot / client） |
|---|---|
| 存储与表结构 | 连 OneBot（只有 bot）或读聊天记录库（client） |
| 增删查改接口 | 群与发送者筛选、按订阅过滤 |
| 读投影：人工修正覆盖机器字段、由截止时间推导状态 | 抽取（规则 + 大模型） |
| 附件二进制存取 | 缺口检测、每日摘要、私聊指令 |
| 幂等与唯一性约束 | 统计计数 |

前端需要的数据全部从后端取；「系统状态」页那部分运行时信息由 bot 提供
（见 `docs/api.md` 第 9 节，它只认管理令牌）。

> **两条入库链路任选一条**（同一个 QQ 账号不要同时开，会重复入库 —— 两边的
> `message_id` 格式不同，幂等键拦不住）：bot 连 OneBot 实时入库；client 读一份
> 聊天记录库、用用户自己的 UserToken 入库，**不连 QQ**。只想用 client 的话，
> 把 bot 的群/发送者白名单留空，它仍然负责 `/注册`、`/订阅`、摘要推送。

## 部署

### Docker（推荐）

镜像由 CI 构建推送到 GHCR，编排在
[`xcollector-deploy`](https://github.com/Xqy1y4ever/xcollector-deploy) 仓库里：

```bash
git clone https://github.com/Xqy1y4ever/xcollector-deploy.git
cd xcollector-deploy
cp .env.example .env     # 填 API_TOKEN / SIGNUP_MODE / 群白名单
sh preflight.sh          # 预检：端口、配置、镜像
docker compose up -d
```

后端只发布到 `127.0.0.1:8000`，由宿主机上的反向代理对外提供 `/api/`。
数据（SQLite + 附件）落在 `backend-data` 卷里，容器可以随便重建。

### 本地直接跑

```bash
python -m venv .venv
.venv\Scripts\activate                # Windows
pip install -r requirements.txt

copy .env.example .env                # 所有项都有默认值，本地可以先不改
python -m app.main
# 交互式接口文档：http://127.0.0.1:8000/docs
```

只跑后端不会有数据进来 —— 还需要另开一个终端跑
[`xcollector-bot`](https://github.com/Xqy1y4ever/xcollector-bot)。

## 配置

全部见 [`.env.example`](.env.example)，均带默认值。

| 配置 | 默认 | 说明 |
|---|---|---|
| `API_TOKEN` | 空 | **服务令牌**。只有 bot 有；本地开发可留空（不校验） |
| `SIGNUP_MODE` | `invite` | 谁能注册：`invite`（要邀请码）/ `open`（谁都能注册） |
| `VERIFY_CODE_TTL` | `600` | QQ 验证码有效期（秒） |
| `VERIFY_MAX_ATTEMPTS` | `5` | 验证码猜错几次作废 |
| `ALLOW_TOKEN_ROTATION` | `true` | 允许已注册用户自助换一个新 UserToken |
| `DB_PATH` | `data/xcollector.db` | SQLite 路径 |
| `ATTACHMENT_DIR` | `data/attachments` | 附件目录 |
| `MEDIA_MAX_BYTES` | `5242880` | 单个附件上限，超限返回 413 |
| `SERVER_HOST` / `SERVER_PORT` | `127.0.0.1` / `8000` | 监听地址 |
| `CORS_ORIGINS` | 本地 5173 | 允许的前端来源 |
| `ATTACHMENT_URL_TTL` | `3600` | 附件签名链接有效期（秒） |
| `ATTACHMENT_SIGN_KEY` | 空 | 附件签名密钥，留空则从 `API_TOKEN` 派生 |
| `LOG_LEVEL` / `LOG_PREVIEW_CHARS` | `INFO` / `60` | 日志 |

**身份有两种，而且不是"权限等级"**（见 [`docs/api.md`](docs/api.md) 的通用约定）：

| 令牌 | 谁持有 | 能做什么 |
|---|---|---|
| `API_TOKEN` | **只有 bot** | 共享层全部；按用户的接口**必须显式带 `user_id`** |
| `UserToken`（`xc_…`） | 每个用户自己 | 只能读写**他自己**的数据；`user_id` 参数被强制忽略 |

**`UserToken` 不是配置项**：用户在 QQ 里给机器人发 `/注册` 拿到验证码，
在网页上完成注册后由 `POST /api/register` 签发。明文只出现那一次
（库里只存 sha256），丢了只能重复同样的流程再换一个。

之所以不再有一个"网页令牌"：那是一个**共享密钥**，所有拿它的人看到的东西完全一样，
既没有审计也没法单独吊销某个人。每人一个令牌之后，"隔离"这件事才第一次有了
强制力 —— 后端对每个按用户的查询强制带 `user_id`，有自检脚本守着这条
（`tests/check_scoping_guard.py`）。

跟 QQ、白名单、抽取、摘要有关的配置**不在这里** —— 那些属于 bot，
放在后端只会让人改了之后困惑为什么没生效。

## 接口

前缀 `/api`，全部端点的请求/响应示例见 [`docs/api.md`](docs/api.md)。

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/messages` | 创建原始消息（幂等：`(group_id, message_id)`） |
| GET | `/api/messages` | 列表：`state` / `group_id` / `since` / `limit` / `count_only=1` |
| GET | `/api/messages/{id}` | 单条（含正文、附件、原始数据、处理状态） |
| PATCH | `/api/messages/{id}` | 只改处理状态与附件，其余字段忽略 |
| POST | `/api/notifications` | 创建或更新（幂等：`raw_message_id`）；证据为空 → 400 |
| GET | `/api/notifications` | 列表（**读投影**）：`since` / `status` / `q` / `limit` / `count_only=1` |
| GET | `/api/notifications/{id}` | `{notification, raw}` |
| PATCH | `/api/notifications/{id}` | 改机器字段；`status` / `read` 会被忽略 |
| DELETE | `/api/notifications/{id}` | 删除 |
| POST | `/api/notifications/{id}/corrections` | 人工修正（只追加） |
| GET | `/api/notifications/{id}/corrections` | 修正历史 |
| POST | `/api/notifications/{id}/read` | 已读 / 未读 |
| POST | `/api/attachments` | `multipart/form-data` 上传，超限 → 413 |
| GET | `/api/attachments/{id}` | 下载附件；支持短时效签名链接 |
| POST / GET | `/api/groups` | 群状态（upsert 时返回上一条消息时间，供缺口检测用） |
| POST / GET | `/api/gap-alerts` | 缺口告警 |
| POST | `/api/gap-alerts/{id}/ack` | 确认告警 |
| POST / GET | `/api/stats` | 流水线计数（后端不理解字段含义，只累加） |
| POST / GET | `/api/digest-log` | 每日摘要发送记录（幂等：`(day, kind, sent)`） |
| PUT / GET / DELETE | `/api/state/{namespace}/{key}` | bot 的键值暂存（带 TTL） |
| GET | `/api/state/{namespace}` | 列出未过期的键值 |
| GET | `/api/health` | 健康检查（只报存储自身） |

## 数据

| 表 | 可变性 | 说明 |
|---|---|---|
| `raw_message` | 只追加 | **原始消息，唯一不可再生的资产**。正文写进去就不再改 |
| `notification` | 可整表重建 | 从原始消息派生出来的通知条目 |
| `correction` | 只追加 | 人工修正。展示时覆盖机器字段，所以重跑不会冲掉人改过的截止时间 |
| `read_state` | 可覆盖 | 已读状态 |
| `attachment` | 只追加 | 附件元数据；二进制在 `ATTACHMENT_DIR` |
| `group_state` | 可覆盖 | 群最后消息时间与今日计数 |
| `gap_alert` | 可追加 / 确认 | 缺口告警 |
| `pipeline_stat` | 累加 | 计数 |
| `digest_log` | 幂等追加 | 摘要发送记录 |
| `bot_state` | 可覆盖 | bot 的键值暂存，带过期时间 |

数据库启动时自动做增量迁移，老库直接升上来，不用手工改表。

## 自检

```bash
python -m tests.check_config        # 配置项都有引用、两个仓库不越界
python -m tests.check_corrections   # 人工修正不会被重跑覆盖
python -m compileall -q app tests   # 语法

# HTTP 端到端（需要另开终端跑着后端，详见文件头部注释）
python -m tests.check_api
python -m tests.check_auth_scopes   # 权限分级与附件签名链接
```
