# Xcollector Backend

把 QQ 官方通知群里发布的通知，抽取为**带 DDL 的任务条目**，写进 SQLite，并通过 HTTP API 提供给前端。

> 完整设计见 [`docs/design.md`](docs/design.md)。本文件只讲怎么跑起来。

Python **3.12+**。

---

## 这个 MVP 做了什么 / 没做什么

**做**：接入 OneBot（NapCat）→ 原始消息无损入库 → 白名单筛选 → 抽取任务与 DDL → 人工可修正 → 每日 digest 私聊推送 → 断线缺口告警。

**不做**：水群/公众号/媒体的信息处理（信噪比太低，本版本只处理官方通知）；知识库；`.ics` 日历导出；文件类附件（docx/xlsx）的内容解析（**只落地保存，不解析**，见下方「已知边界」）。

---

## 在整套系统里的位置

```
QQ/NapCat ──OneBot WS──▶ xcollector-bot ──HTTP POST /api/ingest/messages──▶ xcollector-backend
                              ▲                                                    │
                              └──────── HTTP POST /api/send/private ◀───────────────┤
                                                                                   │
                                                              xcollector-web ◀─────┘
```

**本服务不认识 OneBot 协议。** OneBot/NapCat 连接、消息段解析、合并转发展开、
私聊指令全部在 [`xcollector-bot`](../xcollector-bot/) 里；后端只消费归一化后的消息，
并通过 `BOT_BASE_URL` 调 bot 发消息。这样两边可以分别部署、分别重启，
换掉 QQ 实现也不影响抽取和存储。

---

## 快速开始

```bash
# 1. 建虚拟环境并装依赖
python -m venv .venv
.venv\Scripts\activate            # Windows
pip install -r requirements.txt   # 核心依赖，不含 litellm

# 2. 复制配置
copy .env.example .env
#    至少改这两项：GROUP_WHITELIST / DIGEST_TARGET_QQ

# 3. 不接 QQ、不接 bot，用演示数据验证整条链路（无需 API key）
python -m app.tools.seed_demo --reset --extractor rule

# 4. 起服务
python -m app.main
#    API:  http://127.0.0.1:8000/docs
#    前端: 另开一个终端跑 xcollector-web
#    bot:  另开一个终端跑 xcollector-bot（不跑就收不到 QQ 消息）
```

启动时会探测一次 bot 连通性：bot 没起来会打一条 WARNING，但服务照常运行
（演示数据和前端仍可用）。

---

## 与 bot 的两个方向

### bot → 后端：`POST /api/ingest/messages`

bot 把 QQ 消息规范化成下面这个结构推过来（合并转发已在 bot 侧展开成纯文本）：

```json
{
  "messages": [
    {
      "source": "qq",
      "message_id": "12345",
      "group_id": "673504310",
      "group_name": "NOVA官方通知群",
      "sender_id": "10001",
      "sender_name": "李老师",
      "ts": 1757692800000,
      "text": "@全体成员 大家下周三前把军训心得交到班长那里，不少于800字。",
      "at_all": true,
      "mentions": [],
      "reply_to": null,
      "attachments": [{"type": "image", "url": "https://...", "name": null, "size": null}],
      "raw": {}
    }
  ]
}
```

**群白名单和发送者白名单在后端判定**，所以 bot 不需要知道你的策略，照单全推即可。
响应会给出 `accepted / duplicates / filtered / skipped / errors` 的计数。

附件由**后端**下载落盘（bot 只透传 URL）——文件必须存在后端这一侧，
否则前后端分开部署时前端就取不到图了。

### 后端 → bot：发送与状态

| 后端调用 | 用途 |
|---|---|
| `POST {BOT_BASE_URL}/api/send/private` | 每日 digest |
| `POST {BOT_BASE_URL}/api/send/group` | （当前未使用，留给后续） |
| `GET {BOT_BASE_URL}/api/status` | 健康页显示连接状态 |

认证：bot 推过来时带 `INGEST_API_TOKEN`，后端调过去时带 `BOT_API_TOKEN`。
四个值（两边的两份）要一一对上；都不填则不校验，仅限本地开发。

---

## 掉线期间的漏洞

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

## 日志

**每一条被处理的消息都会打印一行**结构化记录，便于 `grep`：

```
20:09:13 INFO    xcollector.message | 结果=extracted | 群=NOVA官方通知群(673504310) |
发送者=李老师 | msg_id=seed-01 | 发送时间=09-16 16:49:13 | 标题=提交军训心得 |
截止=09-23 23:59(下周三前) | 置信度=0.7 | 抽取器=llm | 模型=deepseek/deepseek-flash |
tokens=812 | 附件=1 | 原文=@全体成员 大家下周三前把军训心得交到班长那里…
```

（上面为了可读性折了行，实际输出是**一行**。）

`结果=` 的取值与数据库 `raw_message.state` 一一对应：

| 结果 | 级别 | 含义 |
|---|---|---|
| `extracted` | INFO | 成功建条。附带标题、截止时间（含原文说法）、置信度、模型、token 数、附件数 |
| `noise` | INFO | 判定为闲聊/回执。**属于正常结果**，不计入盲区 |
| `skipped_whitelist` | INFO | 发送者不在白名单。消息已入库，只是不抽取 |
| `group_filtered` | DEBUG | 群不在白名单。连库都不进 |
| `duplicate` | DEBUG | 重复推送（重连后常见） |
| `unparsed` | **WARNING** | 本该抽出却没抽出（如 evidence 为空被拒绝建条）—— 真盲区 |
| `degraded` | **WARNING** | LLM 失败且规则也没兜住 —— 真盲区 |
| `error` | **WARNING** | 流程抛出未预期异常 |

把 `LOG_LEVEL` 改成 `DEBUG` 就能同时看到 `group_filtered` 和 `duplicate` 这两类
（它们量大且重复，默认不显示）。`LOG_PREVIEW_CHARS` 控制原文预览长度。

> `noise` 与 `unparsed` 是分开的：闲聊不该让"未能解析"这个数字虚高，
> 否则盲区面板就失去了意义 —— 一个总是报警的数字等于没报警。

---

## 自检

```bash
# 中文时间解析回归（18 条断言，锚点固定，不随运行日期漂移）
python -m tests.check_timeparse

# 地点抽取回归（14 条；一半用例期望「抽不出来」，因为错抽比漏抽更糟）
python -m tests.check_location

# 人工修正不被重跑抽取覆盖
python -m tests.check_corrections

# 每条消息恰好一行日志（6 种结局各一条）
python -m tests.check_message_log

# 注入演示数据跑通全链路
python -m app.tools.seed_demo --reset --extractor rule

# HTTP 接口冒烟（需要另开一个终端跑着后端，且必须用 EXTRACTOR=rule）
#   $env:EXTRACTOR='rule'; python -m app.main
#   $env:SMOKE_BASE='http://127.0.0.1:8000'; python -m tests.check_api
```

---

## API

前缀 `/api`，交互式文档在 `/docs`。

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/ingest/messages` | **bot 推消息进来的入口**。见上一节 |
| POST | `/tasks/manual` | 手动建任务（QQ 里 `/add` 指令的后端支撑）。`auto_commit=false` 只解析不入库；`force_commit=true` 无论有没有把握都建 |
| GET | `/notifications` | 列表。支持 `since`（增量）、`status`、`q`；响应附带 `blindspots` |
| GET | `/notifications/{id}` | 详情，含 `raw`（原文、附件、原始事件 JSON） |
| POST | `/notifications/{id}/corrections` | 人工修正 `title`/`summary`/`location`/`due_at`/`due_text`/`status` |
| POST | `/notifications/{id}/read` | 已读 / 未读 |
| GET | `/health` | bot 连接状态、各群最后消息时间、流水线统计、缺口告警、盲区 |
| GET | `/digest/preview` | 预览每日摘要文本 |
| POST | `/digest/send` | `{"dry_run": false}` 真的发到 QQ |
| GET | `/config/meta` | 非敏感配置摘要 |

`status` 的取值与含义：

| 值 | 含义 | 前端表现 |
|---|---|---|
| `active` | 待办 | 正常显示 |
| `expired` | 已过截止时间（自动推导，不是存的） | 归入「已过期」组 |
| `done` | 做完了（QQ 里 `/done`，或前端「标记完成」） | 归入末尾的「已完成」组，弱化显示 |
| `archived` | 不是通知 / 误报（QQ 里 `/del`） | 从主列表消失 |

---

## 目录结构

```
app/
  config.py            配置（全部有默认值，缺 .env 也能启动）
  db.py                SQLite schema、增量迁移与数据访问
  materialize.py       correction 覆盖 notification，生成 API 视图
  bot_client.py        调 xcollector-bot 的 HTTP 客户端（发消息、查状态）
  utils.py             ID / 时间换算
  logging_setup.py     日志初始化（入口脚本共用）
  pipeline/
    ingest.py          归一化消息 → 原始层；附件落地；缺口检测
    runner.py          抽取编排（模式选择、降级、分歧标记）
    manual.py          手动建任务（/add 指令），解析没把握时先回问
    extract.py         LLM 抽取（校验、重试、交叉验证）
    rule_extract.py    确定性规则抽取（含地点）
    timeparse.py       中文相对时间解析
    trace.py           每条消息一行日志
    digest.py          每日摘要
    watchdog.py        静默 / 缺口看门狗
  api/routes.py        HTTP 接口
  main.py              FastAPI 入口
  tools/seed_demo.py   演示数据
tests/                 自检脚本
```

> `onebot/`（消息段解析、WS 连接）已经搬到
> [`xcollector-bot`](../xcollector-bot/) —— 后端不再持有 QQ 连接。

---

## 分层铁律

| 层 | 可变性 | 说明 |
|---|---|---|
| `raw_message` | **只追加** | 唯一不可再生的资产。含 bot 送来的完整消息与原始事件 JSON，永不修改 |
| `notification` | 可整表重建 | 派生层。改 prompt、换模型后可重跑。加 `location` 列时走 `ALTER TABLE`，老数据该列为 NULL |
| `correction` | **只追加** | 人工修正。展示时覆盖 `notification`，所以**重跑永远不会冲掉你改过的 DDL** |
| `task_event` 等 | — | 本 MVP 未引入显式任务状态机（见「已知边界」） |

---

## 已知边界（诚实清单）

1. **文件类附件不解析。** xlsx/docx 会下载到本地并在前端可下载，但内容不会被抽取。
   如果 DDL 藏在附件里，只能靠人打开看。这是当前最大的漏信息通道。
2. **没有任务状态机。** 没有"改期 / 取消 / 合并同一条通知的多条消息"。
   目前一条原始消息对应一条通知；同一条通知被重发会生成多条。
   （QQ 里的 `/done`、`/del` 只是改 `status`，不是完整状态机。）
3. **地点抽取以规则为主。** 规则只认「地点：xxx」和「在/到 + 场所词」这两类明确形态，
   认不出就交给 LLM；LLM 也可能给 null。**宁可为空，也不硬猜**——
   用户会照着错的地点跑一趟。前端已支持人工补正。
4. **没有评测集。** `tests/` 里只有时间解析和地点抽取的回归断言。要回答"抽取准确率是多少"，
   需要按设计文档 §7 手工标注 200~300 条真实消息。
5. **未解析 / 降级只是计数**，前端能看到数量，但还不能逐条点开看是哪些消息。
6. **digest 只推私聊**，没有邮件通道。
7. **不适合多进程部署**：SQLite + 单写入进程。要多 worker 请换 Postgres。
8. **`/api/ingest/messages` 没有限流**：bot 推多少就处理多少。目前靠 bot 侧批量推送控制频率。

这些都在 `docs/design.md` 的路线图里有对应条目。

---

## 故障排查

**启动或首次抽取时卡住几十秒，并刷 `LiteLLM: model cost map fetch attempt 1/3 failed`**
litellm 在**导入时**会去 `raw.githubusercontent.com` 拉模型价格表，网络不通时会阻塞导入、
再起后台线程重试 3 次。我们只用 `usage.total_tokens`，不需要这张表，
`app/__init__.py` 已默认设 `LITELLM_LOCAL_MODEL_COST_MAP=True` 强制用包里自带的副本。

本机实测：**导入耗时 87.2s → 2.8s**。

如果你是在自己的代码里直接 `import litellm`（而不是通过 `app` 包），
需要在导入**之前**自行设置这个环境变量：

```bash
set LITELLM_LOCAL_MODEL_COST_MAP=True      # Windows
export LITELLM_LOCAL_MODEL_COST_MAP=True   # Linux/macOS
```

注意 litellm 是**惰性导入**的（第一次抽取时才加载），所以这个延迟表现为
"第一条消息处理起来像卡死了"，而不是启动时慢。

**`aiosqlite` 相关：脚本报错后进程不退出**
aiosqlite 的工作线程不是 daemon。自检脚本必须保证 `await close_db()` 被执行
（`tests/check_corrections.py` 用 `try/finally` 示范了正确写法）。

**装了依赖但 `EXTRACTOR=llm` 报 litellm 不存在**
litellm 在 `requirements-llm.txt` 里，核心依赖不含它。先用 `EXTRACTOR=rule` 跑通链路。

**健康页显示某个群"尚未收到任何消息"**
群号配错，或者 NapCat 没有把该群的消息推过来（检查 NapCat 那边的群订阅设置）。

**Windows 下 `ZoneInfo("Asia/Shanghai")` 报错**
缺 `tzdata` 包。已在 `requirements.txt` 里；若手工装依赖请确认它装上了。
