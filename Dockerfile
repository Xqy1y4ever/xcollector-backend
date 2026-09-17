# 后端：纯数据层（SQLite + 附件存储）
#
# 只用 python:*-slim，不装编译工具链：requirements 里全是纯 Python 或有 wheel 的包。
ARG PYTHON_VERSION=3.12
FROM python:${PYTHON_VERSION}-slim

# tzdata 是必须的：后端判断"今天"用的是服务器本地时区（见 README「已知边界」），
# 容器默认 UTC 会让每日统计在北京时间早上 8 点翻页。
# 装上系统 tzdata 后，TZ 环境变量才会生效。
RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    TZ=Asia/Shanghai

WORKDIR /app

# 依赖单独一层，改代码不会让这层失效
COPY requirements.txt ./
RUN pip install -r requirements.txt

COPY app ./app
COPY tests ./tests

# db 与附件都落在 /app/data（对应 DB_PATH=data/xcollector.db、
# ATTACHMENT_DIR=data/attachments）。必须挂卷，否则容器一删数据就没了。
RUN mkdir -p /app/data
VOLUME ["/app/data"]

EXPOSE 8000

# 带 API_TOKEN 探自己的 /api/health；未配 token 时不带 Authorization 头
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 CMD \
  python -c "import os,sys,urllib.request as u; t=os.environ.get('API_TOKEN',''); r=u.Request('http://127.0.0.1:8000/api/health',headers={'Authorization':'Bearer '+t} if t else {}); sys.exit(0 if u.urlopen(r,timeout=4).status==200 else 1)"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
