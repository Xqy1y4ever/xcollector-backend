"""FastAPI 应用入口。

    uvicorn app.main:app --reload --port 8000
或：
    python -m app.main
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from . import __version__
from .api import router
from .bot_client import close_bot, get_bot
from .config import get_settings
from .db import close_db, init_db
from .logging_setup import setup_logging
from .pipeline.digest import digest_loop
from .pipeline.ingest import close_http
from .pipeline.watchdog import silence_loop, startup_gap_check

_settings = get_settings()
setup_logging()
logger = logging.getLogger("xcollector")

_tasks: list[asyncio.Task] = []


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    await init_db()
    logger.info("数据库就绪：%s", settings.resolved_db_path)
    logger.info(
        "白名单群：%s",
        ", ".join(f"{k}({v})" for k, v in settings.group_whitelist_map.items()) or "（未配置，接收所有群）",
    )
    logger.info("抽取模式：%s", settings.extractor)

    # 后端不再持有 OneBot 连接：消息由 xcollector-bot 推到 POST /api/ingest/messages。
    # 这里只做一次连通性探测，让启动日志能立刻暴露"bot 没起来"。
    bot_status = await get_bot().status()
    if bot_status.get("reachable"):
        logger.info(
            "bot 可达：%s（connected=%s, mode=%s）",
            settings.bot_base_url,
            bot_status.get("connected"),
            bot_status.get("mode"),
        )
    else:
        logger.warning(
            "bot 不可达（%s）：%s —— 收不到 QQ 消息、也发不出 digest。"
            "请确认 xcollector-bot 已启动。",
            settings.bot_base_url,
            bot_status.get("last_error"),
        )

    await startup_gap_check()
    _tasks.append(asyncio.create_task(digest_loop()))
    _tasks.append(asyncio.create_task(silence_loop()))

    try:
        yield
    finally:
        for t in _tasks:
            t.cancel()
        await close_bot()
        await close_http()
        await close_db()
        logger.info("已关闭")


app = FastAPI(title="Xcollector", version=__version__, lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=_settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(router)


@app.get("/")
async def root():
    return {
        "name": "Xcollector",
        "version": __version__,
        "docs": "/docs",
        "api": "/api/notifications",
        # OneBot 连接已经拆到 xcollector-bot；这里只是转发它的状态
        "bot": await get_bot().status(),
    }


def main() -> None:
    import uvicorn

    s = get_settings()
    uvicorn.run(
        "app.main:app",
        host=s.server_host,
        port=s.server_port,
        reload=False,
        log_level="info",
    )


if __name__ == "__main__":
    main()
