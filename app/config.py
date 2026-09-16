"""配置层。

本服务是**纯数据层**（见 docs/api.md）：只负责存储与增删查改，不做任何
业务判断。所以这里只剩三类配置：认证、存储、HTTP/日志。

设计原则：所有配置都有可用默认值，`.env` 缺失时服务仍能启动。

刻意**不在这里**声明的配置，一律属于 bot（QQ 连接、白名单、抽取、digest）：
放一个后端永远不读的配置项，只会让人改了之后困惑为什么没生效。
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

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
    # 整套系统只有这一个共享密钥：bot 调后端时带 `Authorization: Bearer <API_TOKEN>`。
    # 留空 = 不校验（仅本地开发用），启动时会打一条 WARNING。
    api_token: str = ""

    # ---------------- 存储 ----------------
    db_path: str = "data/xcollector.db"
    # 附件二进制落盘目录；库里只存元数据
    attachment_dir: str = "data/attachments"
    # 单个附件的字节上限，超限上传返回 413
    media_max_bytes: int = 5 * 1024 * 1024

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


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
