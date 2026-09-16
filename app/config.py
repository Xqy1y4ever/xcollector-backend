"""配置层。

设计原则：所有配置都有可用默认值，`.env` 缺失时服务仍能启动，
这样"最小可行性验证"不会被配置问题卡住。
"""

from __future__ import annotations

from datetime import timedelta, timezone, tzinfo
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent


def parse_id_name_pairs(raw: str) -> list[dict[str, str]]:
    """解析 `id:名称,id:名称` 形式的配置。

    名称可省略（只写 id），冒号也支持中文全角。
    """
    items: list[dict[str, str]] = []
    for chunk in (raw or "").replace("，", ",").split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        name = ""
        for sep in (":", "："):
            if sep in chunk:
                id_part, name = chunk.split(sep, 1)
                break
        else:
            id_part = chunk
        id_part = id_part.strip()
        if not id_part:
            continue
        items.append({"id": id_part, "name": name.strip() or id_part})
    return items


def _fallback_tz() -> tzinfo:
    """Windows 上如果没有 tzdata 包，ZoneInfo 会失败，退化为固定 UTC+8。"""
    return timezone(timedelta(hours=8))


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(BASE_DIR / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ---------------- Bot（OneBot 连接已拆到独立的 xcollector-bot 仓库）----------------
    # 后端不认识 OneBot 协议，所有触达 QQ 的动作都走这几个配置指向的 HTTP 接口。
    bot_base_url: str = "http://127.0.0.1:8082"
    bot_api_token: str = ""
    bot_timeout: float = 15.0
    # bot 推消息进来时要带的令牌；为空则不校验（仅本地开发）
    ingest_api_token: str = ""

    # ---------------- 白名单 ----------------
    group_whitelist: str = ""
    sender_whitelist: str = ""
    sender_whitelist_mode: Literal["strict", "off"] = "strict"

    # ---------------- 抽取 ----------------
    extractor: Literal["llm", "rule", "both"] = "llm"
    llm_primary_model: str = "deepseek/deepseek-chat"
    llm_secondary_model: str = ""
    llm_temperature: float = 0.0
    llm_max_retries: int = 2
    llm_timeout: int = 60
    vlm_enabled: bool = False
    vlm_max_images: int = 3

    # ---------------- digest ----------------
    digest_enabled: bool = True
    digest_time: str = "21:30"
    digest_tz: str = "Asia/Shanghai"
    digest_target_qq: str = ""

    # ---------------- 存储 ----------------
    db_path: str = "data/xcollector.db"
    attachment_dir: str = "data/attachments"
    media_download_enabled: bool = True
    media_max_bytes: int = 5 * 1024 * 1024

    # ---------------- 阈值 ----------------
    gap_alert_hours: float = 2.0
    low_confidence_threshold: float = 0.6

    # ---------------- HTTP ----------------
    server_host: str = "127.0.0.1"
    server_port: int = 8000
    cors_origins: str = "http://localhost:5173,http://127.0.0.1:5173"

    # ---------------- 日志 ----------------
    # INFO 时每条消息一行；改成 DEBUG 还能看到"群不在白名单"和"重复推送"这两类。
    log_level: str = "INFO"
    # 消息日志里原文预览的最大长度
    log_preview_chars: int = 60

    forward_max_depth: int = 3

    # ---------------- 派生属性 ----------------

    @property
    def group_whitelist_map(self) -> dict[str, str]:
        return {item["id"]: item["name"] for item in parse_id_name_pairs(self.group_whitelist)}

    @property
    def sender_whitelist_map(self) -> dict[str, str]:
        return {item["id"]: item["name"] for item in parse_id_name_pairs(self.sender_whitelist)}

    @property
    def group_display_names(self) -> dict[str, str | None]:
        """群白名单的显示名；**没写名字时返回 None 而不是把群号当名字**。

        `GROUP_WHITELIST=673504310` 这种写法很常见，如果直接把 id 当名字，
        健康页就会显示「群名：673504310」，看起来像解析错了。
        返回 None 之后前端会退化成显示群号，语义清楚得多。
        真实群名由 bot 通过 get_group_info 随消息带过来。
        """
        return {k: (None if v == k else v) for k, v in self.group_whitelist_map.items()}

    @property
    def group_ids(self) -> set[str]:
        return set(self.group_whitelist_map.keys())

    @property
    def resolved_db_path(self) -> Path:
        p = Path(self.db_path)
        return p if p.is_absolute() else BASE_DIR / p

    @property
    def resolved_attachment_dir(self) -> Path:
        p = Path(self.attachment_dir)
        return p if p.is_absolute() else BASE_DIR / p

    @property
    def digest_hhmm(self) -> tuple[int, int]:
        try:
            hh, mm = self.digest_time.strip().split(":")
            return max(0, min(23, int(hh))), max(0, min(59, int(mm)))
        except Exception:
            return 21, 30

    @property
    def tz(self) -> tzinfo:
        try:
            from zoneinfo import ZoneInfo

            return ZoneInfo(self.digest_tz)
        except Exception:
            return _fallback_tz()

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def cross_check_enabled(self) -> bool:
        return bool(self.llm_secondary_model.strip()) and (
            self.llm_secondary_model.strip() != self.llm_primary_model.strip()
        )

    def in_group_whitelist(self, group_id: str) -> bool:
        """群白名单为空时不做限制（方便首次跑通）。"""
        if not self.group_whitelist_map:
            return True
        return str(group_id) in self.group_whitelist_map

    def in_sender_whitelist(self, sender_id: str) -> bool:
        if self.sender_whitelist_mode == "off" or not self.sender_whitelist_map:
            return True
        return str(sender_id) in self.sender_whitelist_map


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
