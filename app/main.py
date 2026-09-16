"""FastAPI 应用入口。

    uvicorn app.main:app --reload --port 8000
或：
    python -m app.main
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, WebSocket
from fastapi.middleware.cors import CORSMiddleware

from . import __version__
from .api import router
from .config import get_settings
from .db import close_db, init_db
from .onebot import get_hub
from .pipeline.digest import digest_loop
from .pipeline.ingest import close_http, handle_event
from .pipeline.watchdog import silence_loop, startup_gap_check

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
    datefmt="%H:%M:%S",
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("LiteLLM").setLevel(logging.WARNING)
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

    hub = get_hub()
    hub.set_event_handler(handle_event)
    await hub.start()
    if settings.onebot_mode == "server":
        logger.info(
            "反向 WS 已监听：%s%s（请把 NapCat 的反向 WebSocket 指到这里）",
            settings.onebot_listen_host,
            settings.onebot_listen_path,
        )

    await startup_gap_check()
    _tasks.append(asyncio.create_task(digest_loop()))
    _tasks.append(asyncio.create_task(silence_loop()))

    try:
        yield
    finally:
        for t in _tasks:
            t.cancel()
        await hub.stop()
        await close_http()
        await close_db()
        logger.info("已关闭")


app = FastAPI(title="Xcollector", version=__version__, lifespan=lifespan)

_settings = get_settings()
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
        "onebot": get_hub().status(),
    }


if _settings.onebot_mode == "server":

    @app.websocket(_settings.onebot_listen_path)
    async def onebot_reverse_ws(websocket: WebSocket):
        """NapCat「反向 WebSocket」的目标地址。"""
        await get_hub().attach_server_ws(websocket)


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
