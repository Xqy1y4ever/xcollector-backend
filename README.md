# Xcollector Backend

把 QQ 官方通知群里发布的通知，抽取为**带 DDL 的任务条目**，写进 SQLite，并通过 HTTP API 提供给前端。

> 完整设计见 [`docs/design.md`](docs/design.md)。本文件只讲怎么跑起来。

Python **3.12+**。

---

## 这个 MVP 做了什么 / 没做什么

**做**：接入 OneBot（NapCat）→ 原始消息无损入库 → 白名单筛选 → 抽取任务与 DDL → 人工可修正 → 每日 digest 私聊推送 → 断线缺口告警。

**不做**：水群/公众号/媒体的信息处理（信噪比太低，本版本只处理官方通知）；知识库；`.ics` 日历导出；文件类附件（docx/xlsx）的内容解析（**只落地保存，不解析**，见下方「已知边界」）。

---

## 快速开始

```bash
# 1. 建虚拟环境并装依赖
python -m venv .venv
.venv\Scripts\activate            # Windows
pip install -r requirements.txt   # 核心依赖，不含 litellm

# 2. 复制配置
copy .env.example .env
#    至少改这三项：GROUP_WHITELIST / SENDER_WHITELIST / DIGEST_TARGET_QQ

# 3. 先不接 QQ，用演示数据验证整条链路（无需 API key）
python -m app.tools.seed_demo --reset --extractor rule

# 4. 起服务
python -m app.main
#    API:  http://127.0.0.1:8000/docs
#    前端: 另开一个终端跑 xcollector-web
```

跑通之后再接 NapCat（见下）和 LLM。

---

## NapCatQQ 配置（你自己做，本服务只负责连）

本服务**不管理 NapCat**，只按 [OneBot 11](https://github.com/botuniverse/onebot-11) 协议连上去。
NapCat 的安装与登录请参考它的官方文档；这里只说明**网络配置这一项**怎么和本服务对上。

两种模式二选一，`.env` 里的 `ONEBOT_MODE` 要和 NapCat 那边的配置对应。

### 模式 A：`ONEBOT_MODE=client`（本服务主动连 NapCat）

1. 在 NapCat 里新建一个 **WebSocket 服务**（正向 WS Server）。
2. 端口填 `3001`；如果设了 Token，把同样的值填到 `.env` 的 `ONEBOT_ACCESS_TOKEN`。
3. `.env` 里 `ONEBOT_WS_URL=ws://127.0.0.1:3001`（NapCat 不在本机就换成实际 IP）。

本服务会自动重连（指数退避，最长 60 秒一次），断线不会退出进程。

### 模式 B：`ONEBOT_MODE=server`（NapCat 连过来）

1. 在 NapCat 里新建一个 **反向 WebSocket**。
2. URL 填 `ws://127.0.0.1:8081/onebot/ws`。
3. `.env` 里保持 `ONEBOT_MODE=server`。

> 两种模式只能用一个。同时开会让同一条消息进来两次（虽然 `(group_id, message_id)`
> 有唯一约束不会重复入库，但没必要）。

### 一定要做的一件事

**用非主号。** NapCat 属于协议实现，QQ 客户端升级后可能失效，也存在账号风险。
用一个小号待在官方群里即可。

### 还有一件事

NapCat 靠**实时事件推送**，历史消息拉取能力有限且不稳定。
所以 **bot 掉线期间的消息会永久消失**。本服务为此做了缺口检测：
重新收到消息时若发现间隔超过 `GAP_ALERT_HOURS`，会生成告警并出现在 digest 尾部。
看到告警请手工爬一次群核对。

---

## 配置说明

全部配置项见 `.env.example`（每项都有中文注释）。最关键的几项：

| 配置 | 说明 |
|---|---|
| `GROUP_WHITELIST` | `群号:群名,群号:群名`。**不在名单里的群，消息连库都不进。** 留空 = 不限制（仅用于调试） |
| `SENDER_WHITELIST` | `QQ:昵称,...`。官方通知发布者通常是固定的几个人 |
| `SENDER_WHITELIST_MODE` | `strict` = 名单外的人只入库不抽取；`off` = 群里所有消息都抽取 |
| `EXTRACTOR` | `rule` / `llm` / `both`，见下 |
| `DIGEST_TARGET_QQ` | 每日摘要推送到哪个 QQ（私聊） |
| `GAP_ALERT_HOURS` | 群静默多久算缺口，默认 2 小时 |

> 白名单是「群 × 发送者」双重的。名单**外**的消息仍然入库、只是不抽取 ——
> 因为它们可能是对通知的补充或追问，而且"什么都没存"是无法事后补救的。

---

## 三种抽取模式

| 模式 | 说明 | 需要 API key |
|---|---|---|
| `rule` | 纯确定性规则 + 中文时间解析。准确率一般，但**能跑通全链路**，也是 LLM 失败时的兜底 | 否 |
| `llm` | litellm 调用模型抽取；失败时自动降级为 `rule` 并记 `degraded` 统计 | 是 |
| `both` | 规则先跑，LLM 覆盖；**两者结论不一致时标 conflict 交给人判断** | 是 |

启用 LLM：

```bash
pip install -r requirements-llm.txt
```

然后在 `.env` 里填 model 与对应厂商的 API key（litellm 直接读各厂商的环境变量）：

```env
EXTRACTOR=llm
LLM_PRIMARY_MODEL=deepseek/deepseek-chat
LLM_SECONDARY_MODEL=openai/gpt-4o-mini   # 留空 = 关闭交叉验证
DEEPSEEK_API_KEY=sk-xxx
OPENAI_API_KEY=sk-xxx
```

### 关于双模型交叉验证

官方通知每天只有几条到几十条，**量小到完全可以跑两遍**。
配置 `LLM_SECONDARY_MODEL` 后，两个模型会各抽一次，比较 `due_at`：

- 一致 → 正常展示
- 不一致 → `conflict = true`，前端标红并把两个结论都列出来让你选

这比任何模型自评的置信度都可靠，因为它是**独立证据**。

### 关于图片

官方通知经常把 DDL 写在图片里，这是"漏掉信息"的最大来源。
默认 `MEDIA_DOWNLOAD_ENABLED=true` 会把图片和文件下载到本地
（NapCat 给的 URL 有时效性，不下载就永久丢失）。

但**下载 ≠ 看懂**。要让模型读图，需要：

```env
VLM_ENABLED=true
LLM_PRIMARY_MODEL=<一个支持视觉的模型>
```

`VLM_ENABLED=false` 时，带图片的通知会以 `[图片]` 占位入库，
并被计入盲区（未解析/低置信度），**不会假装处理成功**。

---

## 自检

```bash
# 中文时间解析回归（18 条断言，锚点固定，不随运行日期漂移）
python -m tests.check_timeparse

# 人工修正不被重跑抽取覆盖
python -m tests.check_corrections

# 注入演示数据跑通全链路
python -m app.tools.seed_demo --reset --extractor rule
```

---

## API

前缀 `/api`，交互式文档在 `/docs`。

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/notifications` | 列表。支持 `since`（增量）、`status`、`q`；响应附带 `blindspots` |
| GET | `/notifications/{id}` | 详情，含 `raw`（原文、附件、原始 OneBot JSON） |
| POST | `/notifications/{id}/corrections` | 人工修正 `title`/`summary`/`due_at`/`due_text`/`status` |
| POST | `/notifications/{id}/read` | 已读 / 未读 |
| GET | `/health` | OneBot 连接、各群最后消息时间、流水线统计、缺口告警 |
| GET | `/digest/preview` | 预览每日摘要文本 |
| POST | `/digest/send` | `{"dry_run": false}` 真的发到 QQ |
| GET | `/config/meta` | 非敏感配置摘要 |

---

## 目录结构

```
app/
  config.py            配置（全部有默认值，缺 .env 也能启动）
  db.py                SQLite schema 与数据访问
  materialize.py       correction 覆盖 notification，生成 API 视图
  utils.py             ID / 时间换算
  onebot/
    segments.py        OneBot 消息段解析（合并转发、附件、@全体成员）
    hub.py             正向 / 反向 WS 连接，自动重连
  pipeline/
    ingest.py          事件 → 原始层；合并转发展开；附件落地；缺口检测
    runner.py          抽取编排（模式选择、降级、分歧标记）
    extract.py         LLM 抽取（校验、重试、交叉验证）
    rule_extract.py    确定性规则抽取
    timeparse.py       中文相对时间解析
    digest.py          每日摘要
    watchdog.py        静默 / 缺口看门狗
  api/routes.py        HTTP 接口
  main.py              FastAPI 入口
  tools/seed_demo.py   演示数据
tests/                 自检脚本
```

---

## 分层铁律

| 层 | 可变性 | 说明 |
|---|---|---|
| `raw_message` | **只追加** | 唯一不可再生的资产。含原始 OneBot JSON，永不修改 |
| `notification` | 可整表重建 | 派生层。改 prompt、换模型后可重跑 |
| `correction` | **只追加** | 人工修正。展示时覆盖 `notification`，所以**重跑永远不会冲掉你改过的 DDL** |
| `task_event` 等 | — | 本 MVP 未引入显式任务状态机（见「已知边界」） |

---

## 已知边界（诚实清单）

1. **文件类附件不解析。** xlsx/docx 会下载到本地并在前端可下载，但内容不会被抽取。
   如果 DDL 藏在附件里，只能靠人打开看。这是当前最大的漏信息通道。
2. **没有任务状态机。** 没有"改期 / 取消 / 合并同一条通知的多条消息"。
   目前一条原始消息对应一条通知；同一条通知被重发会生成多条。
3. **没有评测集。** `tests/` 里只有时间解析的回归断言。要回答"抽取准确率是多少"，
   需要按设计文档 §7 手工标注 200~300 条真实消息。
4. **未解析 / 降级只是计数**，前端能看到数量，但还不能逐条点开看是哪些消息。
5. **digest 只推私聊**，没有邮件通道。
6. **不适合多进程部署**：SQLite + 单写入进程。要多 worker 请换 Postgres。

这些都在 `docs/design.md` 的路线图里有对应条目。

---

## 故障排查

**`aiosqlite` 相关：脚本报错后进程不退出**
aiosqlite 的工作线程不是 daemon。自检脚本必须保证 `await close_db()` 被执行
（`tests/check_corrections.py` 用 `try/finally` 示范了正确写法）。

**装了依赖但 `EXTRACTOR=llm` 报 litellm 不存在**
litellm 在 `requirements-llm.txt` 里，核心依赖不含它。先用 `EXTRACTOR=rule` 跑通链路。

**健康页显示某个群"尚未收到任何消息"**
群号配错，或者 NapCat 没有把该群的消息推过来（检查 NapCat 那边的群订阅设置）。

**Windows 下 `ZoneInfo("Asia/Shanghai")` 报错**
缺 `tzdata` 包。已在 `requirements.txt` 里；若手工装依赖请确认它装上了。
