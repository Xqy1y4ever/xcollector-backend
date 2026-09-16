# Xcollector 设计文档 v0.1

> 一个把群聊/文档等碎片信息源，加工为「有状态的任务条目」与「可检索的知识库」的信息处理 Agent。
>
> 本文档由一次设计评审收敛而来，包含：原则、架构、数据模型、模块契约、关键策略、评测方案、风险登记、路线图。

---

## 1. 背景与目标

### 1.1 问题

信息源（QQ 群、微信群、语雀、共享文档）持续产生消息。原始形态的问题是：

- **碎**：一条 DDL 可能散落在 5 条消息里，且被 200 条闲聊淹没。
- **贵**：注意力是有限资源，逐条爬楼的成本远高于信息本身带来的差值。
- **不可复用**：爬过一遍之后，信息只存在于人脑里，下次要用还得再爬。

**核心命题：碎片化信息不构成价值，除非它被一个框架组织起来。否则维护它的注意力成本必然大于它带来的信息差优势。**

### 1.2 目标输出（两类，成本与风险不对称）

| 输出 | 定义 | 消费者 | 成本 | 精度要求 |
|---|---|---|---|---|
| **任务条目** | 含 DDL、责任方、详细说明、**状态** | 人（要去做） | 低 | **极高** |
| **知识条目** | 可被检索的、保留溯源的归档信息 | 检索（RAG） | 高 | 中 |

### 1.3 非目标（明确不做）

- 不做实时消息推送。实时推送是又一次打断，与目标矛盾。
- 不做微信个人号自动化（见 §9 风险登记 R1）。
- 不做"全自动理解一切"。人保留否决权。
- 知识库不追求覆盖率，只追求**检索命中率**。

### 1.4 成功判据（v0）

1. 连续 7 天，DDL 漏检 ≤ 1 条。
2. 用户日均主动爬群次数下降 ≥ 70%。
3. 用户能回答"系统今天丢了什么"。

---

## 2. 设计原则（从评审提炼，按重要性排序）

### P1 · 召回优先于精确，绝不丢弃

**系统的失败模式不是"总结得不好"，而是"该看到的没看到"。**

一旦"LLM 识别有效性"实现成 `有效→入库 / 无效→丢弃`，就造了一个**单向门**：判错的代价由用户承担，而被丢掉的东西用户看不见。后果不是效果差，而是**信任一次性崩塌**——用户为保险仍要爬群，系统价值归零但维护成本还在。

因此：

- LLM 的角色从**裁判**降级为**排序 + 打标签 + 抽取**。
- 所有消息无条件进入 append-only 原始层；`noise` 也只是一个标签。
- 人工只需做一件事：**一键否决**（极低摩擦）。
- 主指标是**漏检率**，不是准确率。

### P2 · 原始层是一等公民，加工层随时可重建

原始消息是唯一**不可再生**的资产。换模型、改 prompt、调规则之后，必须能 re-process 全部历史。

- `raw_message` 只 append，永不修改、永不删除（含 `raw` 原始 JSON）。
- `extraction` / `task` / `note` 全部是派生层，允许整表重建。
- 每条加工结果都记录 `model` + `prompt_ver` / `ruleset_ver`。

### P3 · 任务是有状态实体，不是一次性抽取

真实场景：同一条通知连发三遍、DDL 改了、"活动取消"、有人追问"所以是周几"。

- 需要**实体归并**（多条消息 → 一个 task）。
- 需要**状态机**与 `update` / `cancel` 事件。
- `task` 是系统中**唯一有状态**的表。

### P4 · 一个错的 DDL 比没有 DDL 更糟

因为错的会被信任。低置信度必须**显式标注**，不能静默展示。输出永远是三元组：`due_at`(结构化) + `due_text`(原文) + `confidence`。

### P5 · 知识库的价值在检索，不在总结

用户不会去"读"知识库，会在需要时"问"它。因此写入时的**元数据**（来源、时间、发言人、群、话题、上下文）比摘要重要得多——**摘要只是索引**。原消息必须可一键点回原文。

### P6 · 按信息源形态分配输出类型

- 群聊 → 信噪比极低 → **产出任务**。
- 长文/文档（语雀、共享文档、邮件）→ **产出知识**。

让同一条流水线干两件事，是把难度叠在一起。v0 只做前者，v1 再接后者，且走独立分支。

### P7 · 系统必须暴露自己的盲区

必须能回答："今天多少条被丢弃、丢在哪一步、哪些群没接进来、哪些抽取低置信度。"

没有它，就无法区分"今天没任务"和"系统瞎了"。这是信任能**慢慢累积**的唯一途径。

### P8 · 出口要落在用户已经在的地方

网站的隐含成本是"用户必须主动去看"。任务类输出的第一出口是 **digest（QQ/邮件）+ 日历 `.ics`**；网站只做知识库检索界面与任务全量管理台。

---

## 3. 总体架构

```
                    ┌─────────────── 接入层 (connectors) ───────────────┐
  QQ群 ──OneBot──▶ │ qq_connector                                      │
  语雀 ──OpenAPI──▶│ yuque_connector            （v1）                  │
  手工 ──HTTP────▶ │ manual_connector (分享即入库 / 人工转发)           │
                    └───────────────────────┬───────────────────────────┘
                                            │  幂等 (source, id)
                                            ▼
                    ┌────────── 原始层 raw_message (append-only) ────────┐
                    │  唯一不可再生资产 · 永久留存 · 含 raw JSON          │
                    └───────────────────────┬───────────────────────────┘
                                            ▼
   ┌─── 派生层（可整表重建）────────────────────────────────────────────┐
   │                                                                    │
   │  S1 规则初筛 screen        （确定性，零成本，ruleset_ver）          │
   │      @全体成员 / 关键词 / 发送者白名单 / 回复链 / 长度阈值          │
   │      → 产出 screening 标签，不丢弃任何行                            │
   │                          ▼                                         │
   │  S2 小模型分档 classify    （便宜模型，全量已筛消息）               │
   │      → task? / info? / noise?  + 粗置信度                          │
   │                          ▼  只对疑似 task/info 的 ~5%              │
   │  S3 强模型抽取 extract     （结构化输出 + 证据片段）                │
   │      → extraction[] (task|update|cancel|info|noise)                │
   │                          ▼                                         │
   │  S4 归并 + 状态机 merge    （确定性 + 模糊匹配 + 事件应用）         │
   │      → task（唯一有状态的表）                                      │
   │                          ▼                                         │
   │  S5 归档 archive           （v1，独立分支）                         │
   │      → note + embedding + source_refs                              │
   └────────────────────────────┬───────────────────────────────────────┘
                                ▼
   ┌─── 发布层 (publishers) ────────────────────────────────────────────┐
   │  digest(每日一条 → QQ私聊/邮件)    ← 任务的第一出口，v0 唯一出口    │
   │  ics(DDL → .ics 进日历)            ← v1                            │
   │  web(任务管理台 + 知识库检索)      ← v1/v2                         │
   └────────────────────────────────────────────────────────────────────┘

   ┌─── 反馈层 feedback ────┐        ┌─── 观测层 observability ────┐
   │ 一键否决/改期/确认      │        │ pipeline_run + drop_log      │
   │ = 免费标注数据          │        │ 盲区面板                     │
   └────────────────────────┘        └──────────────────────────────┘
```

### 3.1 分层职责铁律

| 层 | 可变性 | 幂等键 | 失败时可接受的行为 |
|---|---|---|---|
| raw | 只 append | `(source, id)` | 绝不丢；宁可重复入库 |
| screening / extraction | 可整表重建 | `(message_ids, model, prompt_ver)` | 可失败、可重跑 |
| task | 有状态 | 归并决策 | 必须可人工修正 |
| note | 可重建 | `(source_refs, embed_ver)` | 可失败 |
| feedback | 只 append | — | 绝不丢 |

---

## 4. 数据模型

v0 用 SQLite（单文件、零运维），v1 迁移 Postgres（仅需改 JSON 字段为 `jsonb`）。

```sql
-- ============ 原始层：只追加 ============
CREATE TABLE raw_message (
  id            TEXT PRIMARY KEY,      -- ULID
  source        TEXT NOT NULL,         -- 'qq' | 'yuque' | 'manual'
  channel_id    TEXT NOT NULL,         -- 群号 / 知识库路径
  channel_name  TEXT,
  sender_id     TEXT NOT NULL,
  sender_name   TEXT,
  ts            INTEGER NOT NULL,      -- 毫秒。消息发送时间 —— 相对时间的唯一锚点
  content       TEXT,
  attachments   TEXT,                  -- JSON
  reply_to      TEXT,                  -- raw_message.id
  raw           TEXT NOT NULL,         -- 原始 JSON，永不丢字段
  ingested_at   INTEGER NOT NULL,
  UNIQUE(source, id)
);
CREATE INDEX idx_raw_channel_ts ON raw_message(channel_id, ts);

-- ============ 派生层 A：初筛 ============
CREATE TABLE screening (
  raw_message_id TEXT PRIMARY KEY REFERENCES raw_message(id),
  hit_rules      TEXT NOT NULL,        -- JSON 数组，命中的规则名，可解释
  score          REAL NOT NULL,        -- 0..1
  ruleset_ver    TEXT NOT NULL,
  created_at     INTEGER NOT NULL
);

-- ============ 派生层 B：抽取（可整表重建）============
CREATE TABLE extraction (
  id             TEXT PRIMARY KEY,
  message_ids    TEXT NOT NULL,        -- JSON 数组：支撑该抽取的全部消息
  kind           TEXT NOT NULL,        -- task|update|cancel|info|noise
  target_task_id TEXT,                 -- kind=update/cancel 时指向被修改的任务
  title          TEXT,
  action         TEXT,                 -- 一句话动作，祈使句
  due_at         INTEGER,              -- 结构化截止时间；NULL = 未识别
  due_text       TEXT,                 -- 原文时间表达，如 "下周三前"
  due_confidence REAL,
  owner          TEXT,                 -- 责任方
  confidence     REAL NOT NULL,        -- 总体置信度
  evidence       TEXT NOT NULL,        -- 证据原文片段（必须非空）
  model          TEXT NOT NULL,
  prompt_ver     TEXT NOT NULL,
  created_at     INTEGER NOT NULL
);
CREATE INDEX idx_ext_message ON extraction(message_ids);

-- ============ 状态层：唯一有状态的表 ============
CREATE TABLE task (
  id                  TEXT PRIMARY KEY,
  title               TEXT NOT NULL,
  action              TEXT,
  due_at              INTEGER,
  due_text            TEXT,
  due_confidence      REAL,
  owner               TEXT,
  status              TEXT NOT NULL,   -- tentative|confirmed|done|expired|cancelled
  origin_extraction_id TEXT,
  first_seen          INTEGER,
  updated_at          INTEGER
);

-- task ←→ extraction 多对多（归并的依据）
CREATE TABLE task_extraction (
  task_id       TEXT NOT NULL,
  extraction_id TEXT NOT NULL,
  PRIMARY KEY (task_id, extraction_id)
);

-- 状态变更审计：每一次改期/取消/完成都留痕
CREATE TABLE task_event (
  id                TEXT PRIMARY KEY,
  task_id           TEXT NOT NULL,
  event             TEXT NOT NULL,     -- create|update_due|cancel|done|merge|split
  field             TEXT,
  old_value         TEXT,
  new_value         TEXT,
  source_message_id TEXT,
  ts                INTEGER NOT NULL
);

-- ============ 知识层（v1）============
CREATE TABLE note (
  id          TEXT PRIMARY KEY,
  title       TEXT NOT NULL,
  summary     TEXT,
  tags        TEXT,                    -- JSON 数组
  source_refs TEXT NOT NULL,           -- JSON: raw_message.id / 外部 URL
  embed_ver   TEXT,
  embedding   BLOB,
  created_at  INTEGER NOT NULL
);

-- ============ 反馈层：免费标注数据，从第一天就收集 ============
CREATE TABLE feedback (
  id          TEXT PRIMARY KEY,
  target_type TEXT NOT NULL,           -- task|extraction|digest_item
  target_id   TEXT NOT NULL,
  action      TEXT NOT NULL,           -- not_a_task|due_wrong|confirm|reschedule|done
  payload     TEXT,                    -- JSON，如正确的时间
  user_id     TEXT,
  ts          INTEGER NOT NULL
);

-- ============ 观测层 ============
CREATE TABLE pipeline_run (
  id            TEXT PRIMARY KEY,
  stage         TEXT NOT NULL,         -- screen|classify|extract|merge|publish
  started_at    INTEGER NOT NULL,
  finished_at   INTEGER,
  input_count   INTEGER,
  kept_count    INTEGER,
  dropped_count INTEGER,
  cost_tokens   INTEGER,
  error         TEXT
);

-- 丢弃也要留痕：保证"绝不静默丢弃"
CREATE TABLE drop_log (
  id             TEXT PRIMARY KEY,
  raw_message_id TEXT,
  stage          TEXT NOT NULL,
  reason         TEXT NOT NULL,        -- 规则名 / 模型判定 / 异常
  detail         TEXT,
  ts             INTEGER NOT NULL
);
```

---

## 5. 模块契约

所有 stage 都是**纯函数式**：读上一层、写本层、不修改历史、可重复执行且结果一致。

### 5.1 `connector` — 接入

```
interface Connector {
  name: string
  // 拉取自 last_cursor 之后的消息；实现方保证幂等（(source,id) 唯一）
  fetch(cursor: Cursor | null) -> { messages: RawMessage[], next_cursor: Cursor }
}
```

- **qq_connector**：OneBot 11 协议（NapCat / LLOneBot）。支持按群白名单订阅。断线需要指数退避重连并把 cursor 落盘。
- **manual_connector**：HTTP `POST /ingest`，供"分享即入库"或人工转发使用。是微信场景的替代路径（见 R1）。
- **yuque_connector**（v1）：OpenAPI 轮询文档更新。

> 接入层的唯一职责是**把消息无损搬进来**。任何过滤都不在这一层。

### 5.2 `screen` — 规则初筛（S1）

```
screen(raw_message) -> Screening { hit_rules: string[], score: number }
```

**永不返回"丢弃"**，只返回"命中哪些规则"。规则集版本化（`ruleset_ver`），可回归测试。

初始规则表：

| 规则名 | 触发条件 | 权重 |
|---|---|---|
| `at_all` | 含 @全体成员 / 群公告 | 0.9 |
| `deadline_kw` | 匹配 `截止|DDL|ddl|之前|前完成|报名|统计|接龙|填表|提交|上交|签到` | 0.7 |
| `sender_whitelist` | 发送者在校方/管理员/负责人白名单 | 0.6 |
| `sender_blacklist` | 发送者在降权名单（由 feedback 自动维护） | −0.8 |
| `reply_chain` | `reply_to` 指向高分行 | +0.3 |
| `has_link_or_file` | 含链接 / 文件 / 在线表格 | +0.3 |
| `too_short` | 长度 < 6 且非纯表情 | −0.2 |
| `in_digest_window` | ts 落在当期 digest 窗口 | 门槛 |

`screen` 之后**全量消息仍然入库**，只是分档。

### 5.3 `classify` — 小模型分档（S2）

```
classify(messages: RawMessage[]) -> { raw_message_id, label: task|info|noise, conf }[]
```

- 便宜模型，批量打包（每请求 20~50 条），输出严格 JSON。
- 目的只有一个：**把进入 S3 的量压到 ~5%**。
- `noise` 也要写回 `drop_log`（stage=classify），可事后抽查翻案。

### 5.4 `extract` — 强模型抽取（S3）

输入不是单条消息，而是**消息簇**（同群、时间窗内、回复链相关），因为 DDL 常常跨消息。

```
extract(cluster: RawMessage[]) -> Extraction[]
```

输出**必须**是结构化 JSON，且每条带 `evidence`（原文片段）与 `confidence`：

```json
{
  "kind": "task",
  "title": "提交军训心得",
  "action": "写并提交军训心得（≥800字）",
  "due_at": 1757692800000,
  "due_text": "下周三前",
  "due_confidence": 0.72,
  "owner": "全体大一",
  "confidence": 0.85,
  "evidence": "大家下周三前把军训心得交到班长那里"
}
```

硬性约束：

- `evidence` 为空 → 该条抽取作废（防止模型幻觉出无据任务）。
- `due_at` 与 `due_text` 必须同生共死；只给 `due_text` 不给 `due_at` 是合法的（无法解析时）。
- 严禁让模型输出自然语言段落作为 `due_at`。

### 5.5 `merge` — 归并 + 状态机（S4）

```
merge(new_extractions, existing_tasks) -> { creates, updates, merges, cancels, events }
```

流程：

1. **候选召回**：同 channel + 时间窗 ±14 天 + `due_at` 相近（±3 天）→ 取 top-K 候选 task。
2. **判同**：标题/动作的归一化文本相似度 + 关键实体（owner、地点、表单链接）重合度 → 超过阈值判为同一任务。
3. **应用事件**：
   - `kind=task` + 无同源 → 新建（`status=tentative`）。
   - `kind=task` + 有同源 → 追加 `task_extraction`，若 `due_at` 不同则写 `update_due` 事件，`status=confirmed`。
   - `kind=update` → 覆盖对应字段，写 `task_event`。
   - `kind=cancel` → `status=cancelled`。
4. **状态自动推进**：`now > due_at + 宽限期` → `expired`；收到 `done` feedback → `done`。
5. **人工修正永远优先**：任何被人工改过的字段打 `locked` 标记，后续自动归并不覆盖。

这一步**尽量确定性**，不要全交给 LLM。LLM 只用于"判同"这一个子问题，且结果要缓存。

### 5.6 `publish` — 发布（S5）

```
publish(tasks, notes, window) -> void
```

- `digest`（v0 唯一出口）：每日固定时间，一条消息包含
  - 新增任务（含 DDL、置信度标记）
  - 即将到期（24h 内）
  - 已过期未处理
  - **尾部一行盲区**："本期共 X 条消息，Y 条未通过初筛，Z 条低置信度待确认"
- `ics`（v1）：每个 DDL 生成日历事件；只对有 `due_at` 且 `confidence ≥ 阈值` 的生成，低置信度过 `due_text` 而不写死时间。
- `web`（v1/v2）：任务管理台（状态筛选、一键否决、手动改期）+ 知识库检索。

### 5.7 `observe` — 盲区

```
observe(window) -> BlindSpotReport
```

必须能回答：

- 各 stage 的 `input_count / kept_count / dropped_count`
- `drop_log` 按 reason 聚合
- 每个群的**最后一条消息时间**（超过 N 小时静默 → 连接器挂了，要告警）
- 低置信度条目列表，可点开看原文
- 本期 token 成本

---

## 6. 关键策略细节

### 6.1 相对时间解析

这是最容易出错、也最影响信任的一环。

- **锚点**：`raw_message.ts`（消息发送时间），**不是**当前时间。
- **消歧**：中文的"周三"默认指**发送日所在周**；"下周三"指下一周。同一簇内若同时出现两者，用出现顺序与上下文校验。
- **合法输入**：`下周三前`、`本周五 24:00`、`9月10日`、`明天中午`、`尽快`（→ `due_at=NULL`，保留原文）。
- **输出**：`due_at` 取该表达式的**上界**（"周三前" → 周三 23:59），并记 `due_confidence`（明说日期 → 0.95；相对表达 → 0.7；模糊 → 0.3）。
- **失败即降级**：解析不出来就留 `due_text` + `due_at=NULL`，展示原文，绝不猜。

### 6.2 置信度与展示规则

| `due_confidence` | 展示 | 进日历 |
|---|---|---|
| ≥ 0.9 | 正常显示 | 是 |
| 0.6 ~ 0.9 | 显示 + `~` 前缀 | 是，标记 tentative |
| < 0.6 | 显示原文 `due_text`，标"待确认" | 否 |

### 6.3 成本分层

```
全量消息 (100%)
  → screen 规则         成本 0
  → classify 便宜模型    ~100% 但批量化、短 prompt
  → extract 强模型        ~5%
  → merge 判同          只对候选对，且结果缓存
```

设一个**每日 token 硬上限**，超限时自动降级（只跑 `at_all` + `deadline_kw` 的规则命中项），并在 digest 尾部说明"今日已降级"。

### 6.4 反馈闭环

| 用户动作 | 系统行为 |
|---|---|
| "不是任务" | 写 `feedback`；对 `sender` 累计降权；若同 `title` 模式出现 ≥3 次，写入规则黑名单 |
| "DDL 错了" + 正确时间 | 写 `feedback`；该 task 字段 `locked`；把样本加入评测集 |
| "完成/取消" | 状态流转 + `task_event` |
| 无动作 | 视为弱正样本（不主动学，只统计） |

---

## 7. 评测方案

**这是整个方案里唯一能证伪的环节。没有它，"LLM 识别是否有效"永远只是一个信念。**

### 7.1 评测集

- 从真实群里**随机抽取 200~300 条消息**（不是挑好处理的），手工标注：

```json
{"raw_message_id": "...", "is_task": true, "due_at": 1757692800000, "owner": "全体大一", "note": "跨3条消息"}
```

- 刻意包含：闲聊、表情、广告、无 DDL 的通知、跨消息 DDL、改期、取消。
- 存放为 `evals/dataset.jsonl`，随项目版本管理。

### 7.2 指标

| 指标 | 定义 | v0 目标 |
|---|---|---|
| **DDL 召回率** | 真实任务的 DDL 被抽出的比例 | **≥ 0.95** |
| 任务召回率 | 真实任务被标记为 task 的比例 | ≥ 0.95 |
| 任务精确率 | 推送条目中真为任务的比例 | ≥ 0.50（宁可多推，人工否决便宜） |
| `due_at` 命中率 | 抽出的时间与实际一致（±0 天） | ≥ 0.85 |
| 误报成本 | 每期 digest 中需否决的条目数 | ≤ 3 条/期 |

**注意精确率目标定得很低是刻意的**：符合 P1，用一次点击换一条不漏。

### 7.3 回归

```
xcollector eval --dataset evals/dataset.jsonl --prompt-ver v3
```

任何 prompt / 模型 / 规则集变更都必须跑一遍，与上次对比。指标退化则不允许合并。

---

## 8. 路线图与验收标准

### v0 · 证明"不用爬楼"（1~2 天）

范围：**1 个 QQ 群，只做任务，不做知识库，不要网站。**

- [ ] `qq_connector` + `raw_message` 落库
- [ ] `screen` 规则初筛（8 条规则）
- [ ] `extract` 单轮 LLM 抽取（含证据与置信度）
- [ ] `merge` 简单判同（同群 + 标题相似 + due 接近）
- [ ] `digest` 每日一条 QQ 私聊，尾部带盲区行
- [ ] `drop_log` + `pipeline_run` 观测数据

**验收**：连续 7 天运行，DDL 漏检 ≤ 1 条；每天投入的否决操作 ≤ 3 次。

### v1 · 让它值得信任

- [ ] 任务状态机 + `update`/`cancel` 抽取 + `task_event` 审计
- [ ] `feedback` 前端（一键否决 / 改期）
- [ ] `ics` 日历导出
- [ ] 盲区面板
- [ ] 评测集 + `eval` 命令（≥200 条标注）
- [ ] **语雀 connector** 作为知识库第一个源（结构化、信噪比远高于群聊）

**验收**：评测集上 DDL 召回 ≥ 0.95；人工改期的任务不会被后续自动归并覆盖。

### v2 · 规模与检索

- [ ] 知识库 RAG 检索 + 原文溯源界面
- [ ] 多群多源，按群配置策略
- [ ] 成本分层与降级策略
- [ ] Postgres 迁移

---

## 9. 风险登记

| ID | 风险 | 影响 | 对策 |
|---|---|---|---|
| **R1** | **微信个人号自动化封号** | 封的是个人主号，不可接受 | 不做。改走 `manual_connector`：人工转发到指定 QQ 群，或手机"分享即入库"。牺牲自动化换安全 |
| R2 | LLM 漏检导致信任崩塌 | 系统被弃用 | P1 召回优先 + 盲区可见 + 一键否决 |
| R3 | 幻觉出无据任务 | 用户被误导 | `evidence` 非空为硬约束；无证据即作废 |
| R4 | 同任务重复生成多张卡片 | 列表腐烂 | S4 归并 + `task_extraction` 多对多 |
| R5 | 相对时间解析错误 | 错过 DDL，比没抽出来更糟 | 三元组输出 + 置信度分级 + 低置信度不进日历 |
| R6 | Token 成本失控 | 项目停摆 | 分层过滤 + 每日硬上限 + 超限降级 |
| R7 | 群成员隐私 / 消息落库 | 合规与信任 | 只订阅白名单群；原文本地存储；知识库输出对外前需脱敏 |
| R8 | 维护者换届（学生社团） | 系统腐烂 | 单文件 SQLite、无魔法依赖、本文档 + 一份 README 即可接手 |
| R9 | 连接器静默挂掉 | 以为"今天没任务" | 每个群记录最后消息时间，超时告警进 digest |

---

## 10. 开放问题（待决策）

1. **技术栈**：建议 Python + FastAPI + SQLite（v0），前端 v1 再上。是否有偏好？
2. **digest 出口**：QQ 私聊 / 邮件 / 两者？收件人范围？
3. **群白名单**：v0 选哪个群？（建议选消息量最大、DDL 最多的那个，价值最容易感知）
4. **部署位置**：本地常驻 / 社团服务器 / 云主机？影响 connectors 的稳定性设计。
5. **告知义务**：群成员消息被系统处理，是否需要事先告知？以什么形式？
6. **知识库是否对外公开**：涉及脱敏与版权。

---

## 附录 A · 命名由来

`Xcollector` — 收集的是 **X**（未定义的信息），产出的是**有框架的信息**。名字里的 X 提醒：不与某一种信息源绑架，群聊只是第一个。
