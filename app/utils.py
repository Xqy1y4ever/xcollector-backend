"""通用工具：ID 生成、时间换算。"""

from __future__ import annotations

import secrets
import time
from datetime import datetime, timezone

from .config import get_settings


def now_ms() -> int:
    return int(time.time() * 1000)


def new_id() -> str:
    """时间有序的短 ID（前 13 位为毫秒时间戳），便于按创建顺序排序。"""
    return f"{now_ms():013d}{secrets.token_hex(4)}"


def local_day(ts_ms: int | None = None) -> str:
    """返回配置时区下的 YYYY-MM-DD。"""
    ts = now_ms() if ts_ms is None else ts_ms
    dt = datetime.fromtimestamp(ts / 1000, tz=timezone.utc).astimezone(get_settings().tz)
    return dt.strftime("%Y-%m-%d")


def to_local(ts_ms: int | None) -> datetime | None:
    if ts_ms is None:
        return None
    return datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).astimezone(get_settings().tz)


def iso_local(ts_ms: int | None) -> str | None:
    dt = to_local(ts_ms)
    return dt.isoformat() if dt else None


def parse_iso_to_ms(value: str | None) -> int | None:
    """把 ISO8601（带或不带时区）转成毫秒时间戳。

    不带时区时按配置时区解释 —— 这一条很关键：
    LLM 经常返回 "2025-09-12T23:59:00" 而漏掉偏移量，
    如果按 UTC 解释就会整体偏 8 小时。
    """
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y/%m/%d %H:%M:%S", "%Y-%m-%d"):
            try:
                dt = datetime.strptime(text, fmt)
                break
            except ValueError:
                continue
        else:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=get_settings().tz)
    return int(dt.timestamp() * 1000)


def human_delta(ts_ms: int | None) -> str:
    """给人看的时间差描述，用于 digest。"""
    if ts_ms is None:
        return "无确定时间"
    diff = ts_ms - now_ms()
    future = diff > 0
    mins = abs(diff) // 60000
    if mins < 60:
        text = f"{mins} 分钟"
    elif mins < 60 * 24:
        text = f"{mins // 60} 小时"
    else:
        text = f"{mins // (60 * 24)} 天"
    return f"还剩 {text}" if future else f"已过期 {text}"
