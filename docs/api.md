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
| 附件二进制存取 | 合并转发展开、每条消息的处理轨迹日志 |
| 唯一性约束与幂等 | 缺口检测、每日 digest 组装与发送 |
| 存储健康检查 | 私聊指令、统计计数 |
| **一切需要跨重启存活的状态**：digest 发送记录、指令的待确认、`/list` 编号映射 | **只持有可随时丢弃的内存**：连接对象、群名缓存 |

**后端不认识的词**：OneBot、群白名单、LLM、抽取、digest、盲区。
一旦这些词出现在后端代码里，就是越界了。

> 唯一的例外是**读投影**（见 `GET /api/notifications`）：前端直接消费后端，
> 它必须拿到"人工修正已生效、status 已推导"的视图。这是「查」，不是业务逻辑。

> **另一处例外，而且是有意加的：后端现在认识「QQ 号」这一个词。**
> 多用户之后 QQ 号是**身份锚点**（`app_user.qq`：注册、找回令牌、bot 把
> QQ 侧的身份解析成 `user_id` 都靠它），所以后端校验它的形状
> （`^[1-9]\d{4,11}$`）是合理的，不是越界。
>
> 这个校验还顺带堵住了一类最难发现的故障：订阅写错了（群号填成群名、
> 发送者填成 `*`）**不会报错**，只会"什么都没有" —— 用户以为订上了，
> 其实一直收不到。在入口就拦下来，比让他等一周才发现便宜得多。
>
> 除此之外后端仍然不认识"群"这个概念：`group_id` / `sender_id` 在它眼里
> 只是两个不透明的字符串，它不判断"这是不是一个通知"，也不决定"该不该抽"。

## 通用约定

- 前缀 `/api`
- 认证：所有请求带 `Authorization: Bearer <令牌>`；令牌为空则不校验（仅本地开发）
- **两种身份，不是两种权限等级**（实现见 `app/auth.py`）：

  | 令牌 | 谁持有 | 身份 | 能做什么 |
  |---|---|---|---|
  | `API_TOKEN` | **bot** | `scope=service` | 共享层读写全部；按用户的接口**必须显式带 `user_id`** |
  | `UserToken`（`xc_…`） | 每个用户自己（网页 + **他自己的入库客户端**） | `scope=user` | 只能读写**他自己**的数据；`user_id` 参数被强制忽略 |

  - `UserToken` **不是配置项**：用户在 QQ 里发 `/注册` 拿验证码，在网页上完成注册后
    由 `POST /api/register` 签发，明文只出现那一次（库里只存 sha256）。
  - 权限不足返回 **403**（身份有效但不该调这个接口），令牌缺失/无效返回 **401**。
    前端据此区分"重新登录"和"你没这个权限"。
  - 为什么不再有 `WEB_API_TOKEN`：那是一个**共享密钥**，不是账号体系 ——
    所有拿它的人看到的东西完全一样。多用户改造把它换成了每人一个 UserToken，
    于是"隔离"这件事第一次有了强制力（见下一节）。
- **`user_id` 是 query 参数，不是 body 字段。** 按用户的接口都接受
  `?user_id=usr_xxx`：
  - 服务令牌**必须**显式给。不给 → **400**，detail 会说清原因。
    猜一个 owner 的后果（把通知写给错的人）比报错严重得多。
  - 用户令牌**无视**这个参数，永远用自己的身份 —— 这样"越权读别人"在实现上
    不可能发生，而不是"被检查拦住了"。
- **`GET /api/health` 是唯一允许不带 `user_id` 的按用户接口**：那种情况下它返回
  `"counts": null` 而不是去查"所有用户"（Docker 的 HEALTHCHECK 就是这么调的）。
- 时间戳一律**毫秒整数**（如 `1757692800000`）
- 所有写接口**幂等**：重复提交不会产生重复行
- 未知字段忽略，不报错

### 谁能写什么（加了入库客户端之后的权限模型）

多用户之前只有 bot 一个写入方，所以"能写"就等于"服务令牌"。现在
[`xcollector-client`](https://github.com/Xqy1y4ever/xcollector-client) 让**每个用户
可以在自己的机器上跑一个入库客户端**（读他自己 QQ 的聊天记录库，用他自己的
UserToken 上报）。于是原文有**两层**，写权限按"写的是哪一层"分：

| 写什么 | 服务令牌 | 用户令牌 | 规则 |
|---|---|---|---|
| 按用户的那一层：通知 / 统计 / 缺口告警 / 自己的键值 | ✅ | ✅ | 归属被强制成他自己，碰不到别人，所以直接允许 |
| **按用户的原文**：`user_raw_message`（`POST/PATCH /api/messages`） | — | ✅ | 写进他自己那份。**不需要订阅任何来源** |
| 共享层：`raw_message` / `group_state` | ✅ | ❌ 403 | 共享层是所有人订阅的群的并集，客户端不写它 |
| `attachment` 字节 | ✅ | ✅ | 没有归属可查（字节只存一份），唯一的闸是 `MEDIA_MAX_BYTES` |
| 运维动作：发邀请码/验证码、列用户、改机器字段、投递名单、digest-log | ✅ | ❌ 403 | 这些不是用户的自助操作 |
| **删自己那条通知**（`DELETE /api/notifications/{id}`） | ✅ | ✅ 只限自己的 | 网页任务板上的「删除」；SQL 里带 `user_id`，别人的 → 404 |

**读是不对称的**：

| 读什么 | 服务令牌 | 用户令牌 |
|---|---|---|
| 共享层：`GET /api/messages`、`GET /api/messages/{id}`、`GET /api/groups` | ✅ | ❌ **403** |
| 自己那条通知指向的原文（`GET /api/notifications/{id}` 里的 `raw`） | ✅ | ✅ 只对通知的主人有 |
| 自己的通知 / 订阅 / 统计 / 缺口 / 键值 / `/api/sources` 目录 | ✅ | ✅ |

共享层里是**所有人订阅的所有群**的消息，让任何一个用户读到就是跨群泄露，
所以能写不等于能读。这条规则由 `tests/check_client_permissions.py` 守着。

> **订阅不是客户端的门槛**（2026-09 改掉了旧规则）。旧规则是"用户令牌也写共享层的
> `raw_message`，但必须先证明订阅过这个来源"。它有两个问题：
>
> 1. **订阅表达不了客户端的输入**：订阅的单位是 `(群, 发送者)`、每人上限 200 条、
>    且明确不支持整群订阅；而一个客户端手上是一整个聊天记录库（几十万组合）。
>    要求它先订阅，等于要求它把自己的库先缩到 200 条 —— 那不是权限，是功能不可用。
> 2. **它治不了本**：共表时客户端可以抢先写一行 `(群, message_id)`，而 bot 启动时的
>    崩溃恢复（`GET /api/messages?state=pending`，服务令牌、全站）会把这行捡走、
>    抽取、扇出给别人。
>
> 现在归属写在**表**上：客户端写 `user_raw_message`，bot 写 `raw_message`，
> 越权在 SQL 层面就不可能发生（`user_raw_message` 也在 `USER_SCOPED_TABLES` 里，
> 不带 `user_id` 的查询会被护栏拦下）。订阅回到了它本来的位置：**只影响 bot**
> （决定 bot 抽什么、扇给谁）。
>
> 已知代价要说清楚：`attachment` 没有归属可查，所以拿到任何有效令牌的人都能
> 反复上传，唯一的闸是单文件上限。这套部署本来就是邀请制的小范围使用，
> 先接受这个代价；要收紧就得给每个用户加配额。
- 出错返回 `{"detail": "..."}`，HTTP 4xx/5xx

### 共享层 vs 按用户

多用户之后，数据分成两层。**分层本身就是契约**，不要混淆：

| 层 | 表 | 归属 | 说明 |
|---|---|---|---|
| **共享** | `raw_message`、`attachment`、`group_state` | 无 | bot 写。`raw_message` 是所有用户订阅的**并集**，同一条原始消息只存一份 |
| **按用户** | `user_raw_message`、`notification`、`correction`、`read_state`、`gap_alert`、`pipeline_stat`、`digest_log`、`bot_state`、`subscription` | `user_id` | 各自独立，互不可见 |

由此推出两条必须记住的规则：

1. **共享层的接口不带 `user_id`**：`GET /api/messages`、`GET /api/messages/{id}`、
   `POST /api/attachments`、`POST/GET /api/groups`。
2. **`POST /api/messages` 写哪一层由令牌决定**：服务令牌 → `raw_message`，
   用户令牌 → `user_raw_message`（归属强制成他自己）。两边响应形状一样，
   幂等键分别是 `(group_id, message_id)` 和 `(user_id, group_id, message_id)` ——
   两个人在同一个群里各跑一个客户端，各自存自己那份，谁也顶不掉谁。
3. **一条原始消息会扇出成 N 条通知**（每个订阅者一条），所以 `notification` 的
   幂等键是 **`(user_id, raw_message_id)`** 而不是 `raw_message_id`。
   同一个 raw 被两个人订，就有两行、两个 id、各自独立的读/完成/修正状态。

> `attachment` 属于共享层是因为**字节只存一份**：访问权由"你手上那条签名 URL"
> 决定，而签名绑定签发时的 `user_id` 和过期时间（见 `GET /api/attachments/{id}`）。

---

## 0. 用户与注册

### `POST /api/register` —— 注册 / 轮换令牌（**公开，不需要任何令牌**）

```json
{"qq": "10001", "code": "123456", "invite_code": "inv_xxx", "display_name": "小明"}
```

- 这个接口是**鉴权之外**的唯一入口，安全完全由三样东西担着：
  **邀请码** + **QQ 验证码**（只有能收到机器人私聊的人拿得到）+ **猜错次数上限**。
  改这里之前先想清楚这三点还在不在。
- `code` 是 6 位验证码，由 bot 通过 `POST /api/verify/request` 签发、再**私聊**回本人。
- 已经注册过的 QQ 再走一次 = **轮换令牌**（此时不需要邀请码）；
  服务端可以用 `ALLOW_TOKEN_ROTATION=false` 关掉它。
- 响应：

  ```json
  {"user": {"id": "usr_...", "qq": "10001", "display_name": null,
            "token_hint": "xc_ab12cd", "status": "active",
            "created_at": 1757692800000, "last_seen_at": null},
   "token": "xc_...", "created": true, "notice": "这个令牌只会显示这一次，请立刻保存。…"}
  ```

  `token` 是**明文令牌，只会出现这一次**（库里只有 sha256）。前端必须让用户当场
  复制走，并明确告诉他丢了只能重复同样的流程再换一个。
- 错误：**400** 邀请码不可用 / 验证码不对或过期 / QQ 号格式不对；**409** 已注册且
  不允许轮换；**403** 账号已停用；**422** 请求体格式不对。
  - ⚠️ 验证码错误刻意是 **400 而不是 401**：这是公开接口，而前端的全局拦截器把
    401 当成"登录过期"→ 清令牌 + 跳登录页。填错一个数字不该把用户弹出注册页
    （他本来就没有令牌可清）。

### `POST /api/verify/request` —— 签发验证码（**服务令牌专属**）

```json
{"qq": "10001"}
```

→ `{"qq": "10001", "code": "123456", "expires_at": 1757693400000}`

- **只允许 bot 调**。谁能调谁就能一直刷新别人的验证码，把真正的主人挡在门外
  （拒绝服务），也把 6 位码的猜测窗口拉长。
- bot 拿到码之后必须**自己用 QQ 私聊发给对方** —— 这是整条注册链路的信任基础：
  只有能收到那条私聊的人，才证明得了自己拥有这个 QQ 号。

### `GET /api/me` → `{"scope": "user"|"service", "user": {...}|null}`

前端拿到令牌后第一件事就是调它：令牌对不对一次就知道。
注意**服务令牌也会返回 200**（`scope=service`），所以判断必须看 `scope`，
不能只看状态码 —— 否则一个服务令牌会被当成合法用户登进去。

### `POST /api/invites` / `GET /api/invites` / `GET /api/users` / `GET /api/users/lookup`（**服务令牌专属**）

- `POST /api/invites` `{"note": "给谁的", "max_uses": 1, "ttl_seconds": null}` → 邀请码
- `GET /api/users` → `{"users": [...], "count": N}`，**不发令牌、不发摘要**
- `GET /api/users/lookup?qq=10001` → `{"user": {...}}`，查不到 **404**
  （bot 的身份解析入口：QQ 号是身份锚点，`user_id` 才是数据归属）

---

## 1. 原始消息：共享层 `raw_message` + 按用户 `user_raw_message`

**两层，两张表，列一模一样**：

- `raw_message`（**共享**）：所有用户订阅的并集，一模一样的一条只存一份。
  只有**服务令牌（bot）**能写、能读 —— 里面是所有人订阅的所有群的消息，
  让任何一个用户读到就是跨群泄露。
- `user_raw_message`（**按用户**）：客户端的原始层。用户令牌写它、读它，
  归属强制成他自己，**不需要订阅任何来源**。

`notification.raw_message_id` 指向其中之一：客户端写的通知指向他自己那份，
bot 扇出来的指向共享层。`GET /api/notifications/{id}` 里的 `raw` 两层都认
（先找他自己的那份，再找共享层），但**只对这条通知的主人有**。

> **为什么分两层**：以前是"用户令牌也写共享层，但必须先证明订阅过这个来源"。
> 两个问题：订阅的单位是 `(群, 发送者)`、上限 200 条、不支持整群，表达不了
> "一整个聊天记录库"；而且共表时客户端可以抢先写一行 `(群, message_id)`，
> bot 的崩溃恢复（`GET /api/messages?state=pending`，全站）会把它捡走、扇给别人。
> 现在越权在 SQL 层面就不可能发生（详见通用约定里的权限表）。

> **旧数据不用搬**：老版本里（用订阅当门槛时）用户令牌写进共享层的那些行仍然在
> `raw_message` 里 —— 它们的通知指向它，`GET /api/notifications/{id}` 的兜底
> 照常取得到。新写入一律进 `user_raw_message`。

> 写前日志的落点：入库方收到消息后**第一件事**就是把它 POST 到
> `/api/messages`，然后才去拿附件、抽取。`state=pending` 同时充当"还没处理完"
> 的恢复队列（**只对共享层**：bot 启动时会 `GET /api/messages?state=pending`；
> 客户端那一层的恢复靠它自己的镜像库）。
> DB 驱动的客户端（`xcollector-client`）里，源库本身就是更久的备份，
> 所以它的镜像丢了也只是重扫一遍，而不会丢消息。

原始消息是**只追加**的：`content`/`raw` 一旦写入永不修改，只有 `state` 三个字段可变。

### `POST /api/messages` — 创建（幂等；写哪一层由令牌决定）

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

幂等键：服务令牌是 `(group_id, message_id)`，用户令牌是
`(user_id, group_id, message_id)` —— 已存在时返回已有 `id` 且 `is_new: false`。
**用户令牌不需要订阅**：写的是他自己那份（`user_raw_message`），
和共享层是两张表，谁也顶不掉谁。

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

**写哪一层由令牌决定**（和 POST 一样）：服务令牌改共享层的行，
用户令牌改**他自己那份** —— SQL 里就带着 `user_id`，改到别人的行不可能发生；
共享层里的行对用户令牌是 **404**（不区分"不存在"和"不是你的"，否则可以用它探测
"这个 id 存在吗"）。

响应返回更新后的行。

---

## 2. 通知 `notification`

### `POST /api/notifications` — 创建或更新（按 **`(user_id, raw_message_id)`** 幂等）

`?user_id=usr_xxx`（服务令牌必填）。这里指定的就是**这条通知的收件人**。

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

- **同一个 raw 消息要被扇出成 N 条**（每个订阅者一条）。bot 的流程是：
  先问投递名单（见 `GET /api/subscriptions/routing`），**只抽一次**，
  然后对名单里的每个人调一次这个接口（各带自己的 `user_id`）。
- 已存在时只覆盖**机器字段**，`correction` 不受影响 —— 于是"重跑抽取"永远不会
  冲掉人工修正。
- `evidence` 为空字符串时**返回 400**：后端替 bot 守住这条硬约束（没有证据的条目不许入库）。

### `GET /api/notifications` — 列表（**读投影**）

`?user_id=usr_xxx`（服务令牌必填；用户令牌忽略它）。

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
| `attachments` | 从对应的原文行取 —— 可能是共享层 `raw_message`（bot 写的），也可能是这个用户自己的 `user_raw_message`（他的客户端写的） |
| `extractor` / `model` / `prompt_ver` | 溯源 |
| `raw_message_id` | 对应的原始消息 id（见下面的说明） |
| `source_ts` / `created_at` / `updated_at` | 时间 |

> **`raw_message_id` 是给入库客户端用的**：它靠这个字段建"这条源消息我处理过"的
> 集合，于是在镜像丢失后重扫时能**跳过重新抽取**（不重复花模型的钱）。
> 用户令牌下没有"直接读原文"的接口，所以这条路径是它拿到 raw id 的正当办法。
> 它是这条通知自己的字段，不泄露任何别人的东西 —— 但**别指望**用它去反查共享层，
> 那条路对用户令牌是封死的（见「谁能写什么」）。

### `GET /api/notifications/{id}`

```json
{"notification": { ...读投影对象... }, "raw": { ...原文行... }}
```

`raw` 两层都认：先找**这个用户自己那份**（客户端写的），再找共享层（bot 写的）。
两条都只对这条通知的主人有 —— 别人的通知本来就 404。

### `PATCH /api/notifications/{id}` — 直接改机器字段

bot 重跑抽取时用。可改：`title` / `summary` / `location` / `due_at` / `due_text` /
`due_confidence` / `evidence` / `conflict` / `candidates` / `model` / `prompt_ver`。

**不允许改 `status` 和 `read`** —— 那两个只能走 `corrections` 和 `/read`
（它们要留痕）。传了会被忽略。

响应返回更新后的读投影对象。

### `DELETE /api/notifications/{id}` → `{"deleted": true}`

`?user_id=usr_xxx`（服务令牌必填）。用户令牌**可以**删自己那条（网页任务板上的「删除」
按钮就是这么做的）：SQL 里带着 `user_id`，别人的或不存在的都回 404（不区分，免得被拿来
探测 id）。这是**真删**（只删通知行，`correction` / `read_state` 那些只追加层不动）；
想保留痕迹就用下面的 corrections 把 `status` 改成 `archived`。

### `POST /api/notifications/{id}/corrections` — 人工修正（只追加）

`?user_id=usr_xxx`（服务令牌必填）。**这是按用户的接口里少数允许用户令牌调的**：
用户给自己的条目改标题/截止时间，天经地义。

```json
{"field": "due_at", "value": 1758124740000, "actor": "web"}
```

`field` 只能是 `title` / `summary` / `location` / `due_at` / `due_text` / `status`。
`status` 的值只能是 `active` / `archived` / `done`。

> **两个"用户"字段含义不同，别混**（多用户改造里最容易写错的一处）：
>
> | 字段 | 位置 | 含义 |
> |---|---|---|
> | `user_id` | **query** | **租户**：这条修正属于谁的数据（隔离用） |
> | `actor` | **body** | **谁操作的**：界面上显示"谁改的"（`"web"` / `qq:10001`） |
>
> 旧契约里 body 里的那个字段就叫 `user_id`（表示操作者），现在改名叫 `actor`。
> 把 `actor` 当租户传会**静默**把修正记到别人名下 —— 读的时候还会显示成
> "他改的"，但数据归属已经错了。

响应：`{"ok": true, "notification": { ...读投影对象... }}`

### `GET /api/notifications/{id}/corrections` → 修正历史（按时间正序）

带 `?user_id=usr_xxx`。每条里有 `field` / `value` / `actor` / `ts`。

### `POST /api/notifications/{id}/read` → `{"read": true}` / `{"read": false}`

`?user_id=usr_xxx`（服务令牌必填）。已读状态是**按用户**的：
A 读过不影响 B 的未读计数。

> **不是自己的条目一律 404（而不是 403）。** 403 会泄露"这个 id 存在但不属于你"，
> 那就等于给了对方一个探测别人条目 id 的信号。

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

`url` 是**裸路径**（规范引用），bot 会把它存进 `raw_message.attachments`。
真正对外提供的 URL **不在这里签** —— 签名会过期，存下来的话历史条目的图就打不开了。

上限 `MEDIA_MAX_BYTES`（默认 5MB），超限返回 413。

### `GET /api/attachments/{id}` → 二进制，带正确的 `Content-Type` 与 `Content-Disposition`

**这是全项目唯一允许不带 `Authorization` 头的接口。**

浏览器用 `<img src>` / `<a href>` 取附件时根本带不了那个头，所以这里走
**短时效签名 URL**：读投影（`GET /api/notifications*`、`GET /api/messages/{id}`）
里的 `attachments[].url` 是**每次读取现签**的：

```
/api/attachments/att_xxx?exp=1789651540&u=usr_abc&sig=cbe1b514e6077d1c1490d90adab36d3f
```

`sig` = `HMAC-SHA256(派生自 API_TOKEN 的密钥, "<user_id>.<att_id>.<exp>")` 的前 32 个
hex 字符。**签名同时覆盖 id 和 user_id**：

- 把 A 的合法签名挪到 B 这个附件上 → 拒绝（覆盖了 id）
- 把 URL 里的 `u=` 改成别人 → 拒绝（覆盖了 user_id）

第二条是**多用户下最关键的一行**：不绑 user_id 的话，拿到别人通知里那条链接的人
就能看别人的附件 —— 而那条链接本身看起来完全正常。

- `u=` 必须带上：下载请求**没有 `Authorization` 头**（浏览器 `<img>` 带不了），
  验证方只能从 URL 里知道"这条链接是给谁的"。它进了签名内容，改不动。
- **没有归属就不签名**：`GET /api/messages*` 是共享层、没有 owner，
  所以那里返回的是**裸路径**（要靠 Bearer 才能取）。
  要在页面上显示图片，请用**通知读投影**里的 `url`。

放行条件（任一满足即可）：

1. 没配 `API_TOKEN`（本地开发，与其它接口一致地不校验）
2. 带了有效 `Bearer`（bot、curl 调试走这条）
3. `u` + `exp` + `sig` 签名有效且未过期

有效期 `ATTACHMENT_URL_TTL`（默认 3600 秒）。设为 `0` 则不签名，退回"必须带 Bearer"
—— 那样 `<img>` 会 401。签名密钥可用 `ATTACHMENT_SIGN_KEY` 单独指定，留空则从
`API_TOKEN` 派生。

> 前端不需要为此做任何事：它本来就读 `attachment.url`，签名是后端在读取时加上的。

> 附件字节是**共享的一份**：拿到别人签名 URL 的人能取到那张图。
> 签名 URL 绑定了 user_id 和过期时间，所以正常使用取不到别人的。
> 这是刻意的取舍 —— 每个用户存一份重复的字节更"干净"，但会把存储放大 N 倍。

---

## 4. 群状态 `group_state`

bot 每收到一条消息就 upsert 一次。**缺口检测需要"上一条消息的时间"**，
所以 upsert 会把更新前的值一并返回，省掉一次竞态的读。

- 写：**只有服务令牌**。群状态是共享的（一个群一行，全站一张表）。
- 读（`GET /api/groups`）：**只有服务令牌** —— 群列表是全站的。

> **客户端不写群状态**（用户令牌 → 403）。它的缺口检测用**自己的镜像库**里的
> 时间线（`Mirror.group_seen_ts`）：两个客户端的同一条时间线写进同一张共享表会
> 互相把 `previous_last_msg_ts` 顶掉，缺口告警就会静默地漏。
> 缺口告警本身是**按用户**的（下面第 5 节），客户端照常写。

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

**按用户扇出**：用户只该看到自己订阅的群的缺口，否则是信息泄露。
所有接口都要 `?user_id=usr_xxx`（服务令牌必填）。

### `POST /api/gap-alerts`

```json
{"group_id": "123456789", "group_name": "...", "from_ts": 1757000000000, "to_ts": 1757060000000, "reason": "两条消息间隔 16.7 小时"}
```

响应：`{"id": "gap_xxx"}`

bot 侧的语义：缺口是**群级**事件（"这个群中间断了一段"），所以它先问
`GET /api/subscriptions/routing?group_id=..`（**不给 `sender_id`** = 这个群里任何
发送者），再给名单里的每个人各写一条。没订阅者就不产生告警。

### `GET /api/gap-alerts?acknowledged=false&limit=20` → `{"alerts": [...]}`

### `POST /api/gap-alerts/{id}/ack` → `{"acknowledged": true}`

---

## 6. 流水线统计 `pipeline_stat`

bot 自己数，数完写进来。后端只做累加，不理解每个字段是什么意思。
所有接口都要 `?user_id=usr_xxx`（服务令牌必填）。

**统计是"按用户"的**：一条消息被扇给 N 个人，就给这 N 个人各记一次。
这不是重复计数 —— 从每个用户的角度看，"为我处理了一条消息"确实发生了。
全站视角的数字在运维层面没人需要，而用户视角的数字
（"我的源里有多少条没能解析"）才是盲区告警要用的。

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
  "user_id": "usr_...",
  "storage": {"driver": "sqlite", "path": "data/xcollector.db", "writable": true},
  "counts": {"messages": 1234, "notifications": 87, "attachments": 12},
  "version": "0.2.0"
}
```

- **不带 `user_id` 时 `counts` 是 `null`**，`user_id` 也是 `null`。
  这是唯一允许不带归属的按用户接口：Docker 的 `HEALTHCHECK` 只带 `API_TOKEN`，
  拿不到 user_id。如果这里坚持要 user_id，容器会**永远不健康**，
  compose 里 `depends_on: service_healthy` 的 web 就永远起不来 ——
  一个探针把整套部署卡死。
- 反过来，`counts: null` 也是刻意的：探针**不该**顺带做一次"所有用户"的
  无归属查询。没归属就不查，而不是查了再假装没查。

---

## 7b. 订阅 `subscription`

**这是多用户之后用户唯一能改的东西**：他订哪些「某个群里某个人」。

### 为什么最小单位是 (群, 发送者)，而不是群

这条流水线里值得进清单的东西是**人**发的，不是群发的。允许"订这个群"就等于
允许"这个群里任何人说话都进我的清单" —— 那正是整条工作流要避免的噪声，
而且一旦有人这么订了，LLM 调用量和误报会一起失控。

所以**没有**"整个群"这个选项，而且是三层堵死：表结构 `sender_id NOT NULL`；
参数校验拒绝空值、`*` / `all` / `全部` 这类通配符、逗号分隔的多值；
群号和发送者都必须是 QQ 号形状（写错了立刻报错，而不是安静地存下一条
永远匹配不到任何消息的订阅 —— 后者是用户以为订上了、其实一直收不到，
最难发现的一类故障）。

> 订阅定义"**抽什么**"，bot 的群白名单定义"**看得到什么**"。两者都要满足。

### `GET /api/subscriptions` → `{"subscriptions": [...], "count": N}`

`?user_id=usr_xxx`、`?include_disabled=false`（默认连停用的一起列，
因为前端要能再把它们打开）。

单项形状：`{id, group_id, sender_id, group_name, sender_name, note, enabled,
created_at, updated_at}`。**不含 `user_id`** —— 调用方已经知道那是谁了。

### `POST /api/subscriptions`

```json
{"group_id": "123456789", "sender_id": "10001", "group_name": "示例通知群", "note": "只关注 ddl"}
```

→ `{"subscription": {...}, "created": true}`

- `sender_id` **必填**。只给群号 → 400，detail 会解释为什么不支持订整个群。
- 重复订阅同一个 `(群, 发送者)` 是**幂等**的：不会 409，而是重新启用并返回
  `created: false`（"再订一次"和"把关掉的重新打开"是同一个意图）。
- 名字/备注只在非空时覆盖：重新订阅时前端可能只填了 id，不该把之前记的群名抹掉。
- 上限 `SUB_MAX_PER_USER = 200`（只在真的要**新增**时检查，
  否则订满之后连自己原有的订阅都动不了）。
- **用户令牌可以调**（这是用户配置自己的东西），服务令牌也可以但要显式带 `user_id`
  —— 那是 bot 处理 QQ 侧 `/订阅` 指令时用的路径。

### `PATCH /api/subscriptions/{id}`

body `{"enabled": false, "note": "...", "group_name": "...", "sender_name": "..."}`
（只改传进来的字段；什么都不传 → 400）。→ `{"subscription": {...}}`；
不是自己的 → **404**。

### `DELETE /api/subscriptions/{id}` → `{"deleted": true}`；不是自己的 → 404

**退订不会删掉已有的通知**：历史是历史。它只影响之后的路由。

### `GET /api/subscriptions/routing?group_id=..&sender_id=..` —— **投递名单**（服务令牌专属）

→ `{"user_ids": ["usr_a", "usr_b"]}`

- 来了一条 (群, 发送者) 的消息，**哪些用户要**。bot 每处理一条消息都要问它一次；
  空名单 = 没人要这条消息，那就不该花 LLM 的钱去抽它。
- **省略 `sender_id`** = "这个群里**任何**发送者"。只有缺口告警用它：
  缺口是**群级**事件（"这个群中间断了一段"），凡是订了这个群里任何人的用户都该知道。
  两种语义不要混 —— 正常投递必须给 `sender_id`，否则就成了"订整个群"。
- 返回的只有 `user_id`，没有任何订阅细节：bot 只需要知道"扇给谁"，
  每个人各自的群名/备注是他们的私事。
- **必须服务令牌**：普通用户拿自己的令牌就能看到全局投递名单（谁订了哪个来源），
  那是别人的订阅关系。

### `GET /api/sources?keyword=&limit=` → `{"sources": [...], "count": N}`

**信息源目录**：这套部署目前见过的 `(群, 发送者)` 组合，供用户挑选订阅。

单项：`{group_id, group_name, sender_id, sender_name, last_ts, msg_count}`。

- 新用户注册后手上是空的 —— 没有通知、不知道群号，也就无从订阅。
  没有这份目录，"用户自己配置订阅"根本没法用。
- 目录从**共享层 `raw_message` 聚合**，所以**不含任何 `user_id`**：
  它回答的是"这套部署看得见哪些来源"，而不是"谁收了多少"。
- 代价要说清楚：只有 **bot 实际处理过** 的组合才会出现在这里
  （订阅定义抽什么、白名单定义看得到什么，白名单外的群不会出现）。
- 任何登录用户都能看（`UserToken` 即可）。

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

## 9. bot 暴露的**运营者**运行时状态

「系统状态」页需要 OneBot 连接、今日流水线计数、盲区、缺口 —— 这些
**全在 bot 手里**（后端只剩存储），所以它调的是 bot 而不是后端。

> ⚠️ **这些接口不是给普通用户看的，也不是给浏览器看的。**
> bot 自己的 `/api/*` 只认**管理令牌**（`API_TOKEN` / `BOT_API_TOKEN`），
> 而前端里只有每个用户自己的 UserToken —— bot 不认识那种令牌。
> 状态页是**运营者视角**（里面是所有人的盲区计数、白名单、OneBot 连接状态），
> 普通用户看不到它是设计如此。
>
> 所以：网页上的"系统状态"要么让运营者自己填管理令牌，要么改成本机
> `curl -H "Authorization: Bearer $API_TOKEN" http://127.0.0.1:8082/api/status`。
> 部署时**不要**把 8082 反代到公网（见 `xcollector-deploy`）。

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
    "today_llm_tokens": 18342,
    "per_user_aggregate": true,
    "users_counted": 3
  },
  "blindspots": {
    "unparsed_count": 3,
    "conflict_count": 1,
    "low_confidence_count": 2,
    "degraded_today": false,
    "window_days": 7,
    "per_user": [
      {"user_id": "usr_a", "qq": "10001", "display_name": "小明",
       "stat": {"ingested": 20, "extracted": 3, "unparsed": 1},
       "conflict_count": 1, "low_confidence_count": 0,
       "digest_sent_today": true, "open_gap_alerts": 1,
       "stat_available": true, "conflict_available": true}
    ],
    "users_missing": 0,
    "users_counted": 3,
    "aggregate_note": "标量是按用户累加：一条消息扇给 N 个人会计 N 次"
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
  "digest": {"enabled": true, "time": "21:30", "target_qq": "10001",
             "sent_today": 2, "sent_today_all": false},
  "day": "2026-09-16",
  "server_time": 1757692800000
}
```

**多用户之后这里分两层**，读的时候别混：

- **共享层**（原始消息、群状态、缺口）本来就是全站的，仍然是一个数字。
- **按用户层**（通知冲突、低置信、digest、缺口）按用户各算一份。
  `blindspots.per_user` 是清单；上面那些标量是它们的**累加**。

> ⚠️ **标量不是"今天处理了多少"**：一条消息扇给 N 个人就计 N 次
> （因为它确实替 N 个人各处理了一次）。`pipeline.per_user_aggregate: true`
> 和 `blindspots.aggregate_note` 就是给前端把这件事说出来的。
> 运维要判断"**谁的**源出问题了"只能看 `per_user`。

`blindspots.users_missing > 0` 表示有几个用户的按用户数字**没取到**
（后端部分失败）。必须把它显示出来：否则"0 个冲突"和"没问出来"长得一模一样 ——
那是最误导人的一种绿。

### `GET {BOT}/api/digest/preview?user_id=usr_xxx` → `{"text": "...", "user_id": ..., "qq": ..., "recipients": [...]}`

多用户之后 digest 是**按人**组装的，所以必须知道"预览谁的"。
不传 `user_id` 就用第一个收件人，并在响应里把用的是谁写清楚 ——
预览了一份别人的 digest 却以为是自己的，比不预览更危险。

### `POST {BOT}/api/digest/send` `{"dry_run": true, "user_id": null}`

→ `{"ok": true, "sent": 0, "total": 2, "dry_run": true, "text": "...", "recipients": [{"qq": "10001", "user_id": "usr_a", "sent": false, "error": null}], "error": null}`

- 不传 `user_id` 就按收件人名单**逐个**发（每人一份自己那份）。
- `sent` 是**成功条数**（不是布尔）。`text` 只回第一份，逐份都写进了 `digest_log`。

### `GET {BOT}/api/status` 的认证

**bot 自己的 `/api/*` 现在只认一个令牌**：管理令牌（`BOT_API_TOKEN` / `API_TOKEN`）。
多用户之前还有一个 `WEB_API_TOKEN`（网页令牌）能让登录页读状态页；现在
**前端不再持有任何共享令牌**（每人一个 UserToken，而 bot 不认识 UserToken），
而且状态页本来就是**运营者视角**（里面是所有人的盲区计数）——
普通用户看不到它是设计如此。

所以浏览器里**没有**能调 `/api/status` 的凭据：给它填管理令牌，或者在本机用
`curl -H "Authorization: Bearer $API_TOKEN" http://127.0.0.1:8082/api/status`。

### 附件 URL

后端返回的 `attachments[].url` 形如：

- **读投影里**（`GET /api/notifications*`）：`/api/attachments/att_xxx?exp=…&u=usr_…&sig=…`
  —— 每次读取**现签**，带过期时间，而且 HMAC 内容包含 `user_id`
  （拿到别人那条链接的人验签过不去）。
- **上传响应 / 共享层**（`POST /api/attachments`、`GET /api/messages*`）：**裸路径**。
  那些位置没有"归属"可以绑，所以只能靠 `Authorization` 头 —— 而浏览器
  `<img src>` 带不了头。**要在页面上显示图片，请用通知读投影里的 `url`。**

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

Query：`day`、`kind`、`sent`（`true`/`false`）、`limit`（默认 50）、`count_only=1`。
**必须带 `?user_id=usr_xxx`**（服务令牌）—— "今天发过没有"要按用户问：
A 收到过不代表 B 收到过。混成一个标记会让第二个人的那份被静默吞掉。

→ `{"logs": [...]}` 或 `{"count": n}`

按 `ts` 倒序。`count_only=1` 让 bot 能只问「**这个用户**今天 auto 且 sent=true 的有几条」，
不用把正文全拉回来。

---

## 11. bot 的键值暂存 `bot_state`

指令的「待确认」（`/add` 解析没把握时回问，等用户回 y/n）和 `/list` 的
「编号 → 通知 id」映射，都需要跨重启存活：用户回复 `y` 时如果 bot 刚重启过，
那条待确认不该凭空消失。

**这里刻意做成不透明的键值对**：后端不理解 `value` 的含义，只负责存取和过期清理。
后端因此不需要知道「待确认」是什么东西 —— 它只是一块带 TTL 的持久化草稿纸。

**也必须按用户分**（所有接口 `?user_id=usr_xxx`）：两个用户同时 `/add` 待确认，
共用一份就会互相覆盖 —— 一个人确认掉的可能是另一个人的草稿。

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


