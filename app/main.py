"""FastAPI 应用入口。

    uvicorn app.main:app --reload --port 8000
或：
    python -m app.main

本服务是**纯数据层**：启动时只做两件事 —— 打开 SQLite（含增量迁移）、
检查共享密钥有没有配。没有后台任务、没有向外部的连接：
抓消息、抽取、发 digest 全是 xcollector-bot 的事。
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from . import __version__
from .api import open_router, public_router, router
from .config import get_settings
from .db import close_db, init_db
from .logging_setup import setup_logging

_settings = get_settings()
setup_logging()
logger = logging.getLogger("xcollector")


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    await init_db()
    logger.info("存储就绪：%s", settings.resolved_db_path)
    logger.info("附件目录：%s（单文件上限 %d 字节）", settings.resolved_attachment_dir, settings.media_max_bytes)
    if settings.auth_enabled:
        logger.info("认证已开启：所有 /api 请求都需要 Bearer 令牌")
        logger.info(
            "注册方式：%s",
            "需要邀请码（用 API_TOKEN 调 POST /api/invites 签发）"
            if settings.signups_require_invite
            else "**开放注册** —— 任何能通过 QQ 验证的人都能注册",
        )
        if settings.attachment_url_ttl <= 0:
            logger.warning(
                "ATTACHMENT_URL_TTL <= 0：附件 URL 不做签名，浏览器 <img> 取附件会 401"
            )
    else:
        logger.warning(
            "API_TOKEN 未配置：本服务不校验请求的 Authorization 头（仅限本地开发）。"
            "生产部署请设置 API_TOKEN，并让 bot 用同一个值。"
        )
    try:
        yield
    finally:
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
# 附件下载单独挂：它允许不带 Authorization 头（改由签名 URL 判断），
# 所以不能带上那个"全 router 都要 Bearer"的依赖。见 routes.py 的 require_download。
app.include_router(open_router)
# 注册单独挂：它的前提就是"还没有令牌"，所以必须在鉴权之外。
app.include_router(public_router)


@app.get("/")
async def root():
    """非 /api 的欢迎信息（不含任何数据，因此不需要令牌）。"""
    return {
        "name": "Xcollector",
        "version": __version__,
        "docs": "/docs",
        "api": "/api/notifications",
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
