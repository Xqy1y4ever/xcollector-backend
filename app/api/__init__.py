"""HTTP 路由。

两个 router（理由见 routes.py 顶部的注释）：
  - `router`      —— 要 Bearer，绝大多数接口
  - `open_router` —— 不要 Bearer，自己判断"签名 URL 或 Bearer"，只有附件下载
"""

from .routes import open_router, router

__all__ = ["router", "open_router"]
