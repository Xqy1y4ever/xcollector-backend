# Xcollector Backend

Xcollector 的**数据层**：把 QQ 群里收到的官方通知存下来，提供一套 HTTP 接口给
bot / 客户端写入，给每个用户读取自己的那一份。

它**不做任何业务判断** —— 「什么是通知」「截止时间对不对」由
[`xcollector-bot`](https://github.com/Xqy1y4ever/xcollector-bot) 决定。
后端只负责存、查，以及把**人工修正过的视图**读回去。

- 技术栈：Python 3.12+ · FastAPI · Uvicorn · SQLite（单文件，无外部依赖）
- 存储：数据库文件 + 附件目录，两者都在 `data/` 下
- 多用户：每个用户注册后拿到自己的 `UserToken`，数据按 `user_id` 隔离
- 接口契约：[`docs/api.md`](docs/api.md)（仓库之间唯一的约定）

> 整套系统怎么部署（NapCat、bot、前端、反向代理）见
> [`xcollector-deploy`](https://github.com/Xqy1y4ever/xcollector-deploy) 的 README。
> 本文只讲这个仓库自己怎么跑起来。

## 部署

### Docker（推荐）

镜像由 CI 构建推送到 GHCR，部署脚本在 deploy 仓库里：

```bash
cd xcollector-deploy/backend
cp .env.example .env          # 至少填 API_TOKEN
./start.sh                    # 建网络与数据卷 → 起容器 → 等健康检查
```

镜像名：`ghcr.io/xqy1y4ever/xcollector-backend:latest`。
数据落在 Docker 卷 `xcollector_backend-data`（容器内 `/app/data`），**别删这个卷**。

单独跑（不用那个脚本）：

```bash
docker run -d --name xcollector-backend \
  -p 127.0.0.1:8000:8000 \
  -e API_TOKEN=<服务令牌> -e SIGNUP_MODE=invite \
  -v xcollector_backend-data:/app/data \
  ghcr.io/xqy1y4ever/xcollector-backend:latest
```

### 本地跑

```bash
python -m venv .venv
.venv\Scripts\activate            # Windows；Linux/macOS: source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env              # 至少填 API_TOKEN
uvicorn app.main:app --host 127.0.0.1 --port 8000
```

数据库与附件目录会自动创建，不需要手动建表（启动时做增量迁移，老库直接升上来）。

### 配置

完整说明见 [`.env.example`](.env.example)，常用项：

| 变量 | 默认 | 说明 |
|---|---|---|
| `API_TOKEN` | 空 | **服务令牌**，只有 bot 用（调用 `POST/PATCH /api/messages`、发邀请码等）。**绝不给浏览器、不给用户。** 生成：`python -c "import secrets;print(secrets.token_hex(32))"`。留空 = 不校验任何请求，仅供本机开发 |
| `SIGNUP_MODE` | `invite` | `invite` = 注册要邀请码（用 `API_TOKEN` 调 `POST /api/invites` 签发）；`open` = 任何能过 QQ 验证的人都能注册 |
| `DB_PATH` | `data/xcollector.db` | SQLite 文件 |
| `ATTACHMENT_DIR` | `data/attachments` | 附件二进制目录（库里只存元数据） |
| `MEDIA_MAX_BYTES` | `5242880` | 单个附件字节上限，超限返回 413 |
| `SERVER_HOST` / `SERVER_PORT` | `127.0.0.1` / `8000` | 监听地址（建议只绑本机，由你的反向代理对外） |
| `CORS_ORIGINS` | `http://localhost:5173,...` | 前后端**不同源**时才需要；同源反代时保持默认即可 |
| `ATTACHMENT_URL_TTL` | `3600` | 附件签名链接有效期（秒）。`0` = 不签名，那样 `<img>` 会 401 |
| `ATTACHMENT_SIGN_KEY` | 空 | 留空 = 从 `API_TOKEN` 派生，通常不用配 |
| `VERIFY_CODE_TTL` / `VERIFY_MAX_ATTEMPTS` | `600` / `5` | QQ 验证码有效期与允许猜错次数 |
| `ALLOW_TOKEN_ROTATION` | `true` | 是否允许老用户重新走一次「要码 → 网页提交」来换令牌 |
| `LOG_LEVEL` | `INFO` | 日志级别 |

用户令牌（`UserToken`，`xc_` 开头）**不在这里配**：它由用户在网页上注册时签发，
库里只存摘要。

### 确认在跑

```bash
curl -H "Authorization: Bearer $API_TOKEN" http://127.0.0.1:8000/api/health
```

容器自带 HEALTHCHECK（带 `API_TOKEN` 探 `/api/health`）。接口清单与字段含义见
[`docs/api.md`](docs/api.md)。

## 数据与备份

`data/` 里就是全部：`xcollector.db`（SQLite）+ `attachments/`（附件字节）。
库是单文件、无外部依赖，**停止写入后直接复制目录**即可完成备份：

```bash
# Docker 卷（deploy 里的默认卷名）
docker run --rm -v xcollector_backend-data:/data -v "$PWD":/backup alpine \
  tar czf /backup/xcollector-$(date +%F).tar.gz -C /data .
```

升级：换镜像 tag 重启即可，启动时会自动做数据库增量迁移。

## 许可

MIT
