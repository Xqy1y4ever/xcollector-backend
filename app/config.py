"""配置层。

本服务是**数据层 + 用户系统**：存储与增删查改，以及"谁是谁"。
后者只做认证与注册（见 users.py），不做任何业务判断。

设计原则：所有配置都有可用默认值，`.env` 缺失时服务仍能启动。

刻意**不在这里**声明的配置，一律属于 bot（QQ 连接、订阅、抽取、digest）：
放一个后端永远不读的配置项，只会让人改了之后困惑为什么没生效。
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(BASE_DIR / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ---------------- 认证 ----------------
    # **服务令牌**：只有 bot 有。它能跨用户读写所有人的数据，所以绝不外传。
    # 留空 = 不校验任何请求（仅本地开发用），启动时会打 WARNING。
    api_token: str = ""

    # 用户令牌（UserToken）**不走配置**：每个用户注册时各自签发一个（见 users.py），
    # 绑定到具体的 user_id，只能碰自己的数据。

    # ---------------- 注册 ----------------
    # invite = 需要邀请码（默认）；open = 任何能通过 QQ 验证的人都能注册
    signup_mode: Literal["invite", "open"] = "invite"
    # QQ 验证码有效期（秒）与最多允许猜错几次
    verify_code_ttl: int = 600
    verify_max_attempts: int = 5
    # 是否允许已有用户再走一次「QQ 要码 → 网页提交」来轮换令牌。
    # 令牌只存摘要，丢了拿不回来，所以默认必须有这条路。
    allow_token_rotation: bool = True

    # ---------------- 存储 ----------------
    db_path: str = "data/xcollector.db"
    # 附件二进制落盘目录；库里只存元数据
    attachment_dir: str = "data/attachments"
    # 单个附件的字节上限，超限上传返回 413
    media_max_bytes: int = 5 * 1024 * 1024

    # ---------------- 附件签名 URL ----------------
    # 有效期（秒）。浏览器用 <img> 取附件时带不了 Authorization 头，所以读投影
    # 里的附件 url 是**现签**的，带 exp + HMAC（见 signing.py）。
    # 0 = 不签名，退回"下载必须带 Bearer"（那样 <img> 会 401）。
    attachment_url_ttl: int = 3600
    # 签名密钥。留空 = 从 API_TOKEN 派生，通常不用单独配。
    # 想单独轮换附件链接（不影响登录令牌）时才设它。
    attachment_sign_key: str = ""

    # ---------------- HTTP ----------------
    server_host: str = "127.0.0.1"
    server_port: int = 8000
    cors_origins: str = "http://localhost:5173,http://127.0.0.1:5173"

    # ---------------- 日志 ----------------
    log_level: str = "INFO"
    # 日志里文本预览的最大字符数（写接口的 INFO 日志用）
    log_preview_chars: int = 60

    # ---------------- 派生属性 ----------------

    @property
    def resolved_db_path(self) -> Path:
        p = Path(self.db_path)
        return p if p.is_absolute() else BASE_DIR / p

    @property
    def resolved_attachment_dir(self) -> Path:
        p = Path(self.attachment_dir)
        return p if p.is_absolute() else BASE_DIR / p

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def auth_enabled(self) -> bool:
        """是否真的在校验令牌。空令牌只对本地开发放行，不是"配置好了"。"""
        return bool(self.api_token.strip())

    @property
    def signups_require_invite(self) -> bool:
        return self.signup_mode == "invite"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
