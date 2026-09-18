"""HTTP 路由。

三个 router（理由见 routes.py 顶部的注释）：
  - `router`        —— 要 Bearer，绝大多数接口
  - `open_router`   —— 不要 Bearer，自己判断"签名 URL 或 Bearer"，只有附件下载
  - `public_router` —— 完全不鉴权，只有注册（注册的前提就是还没有令牌）
"""

from .routes import open_router, public_router, router

__all__ = ["router", "open_router", "public_router"]
