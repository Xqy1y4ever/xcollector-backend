"""通用工具：ID 生成、时间换算、日志预览。

刻意保持很小：一旦这里开始出现"解析时间表达""判断是不是通知"之类的函数，
就说明业务逻辑又漏回后端了（那些在 xcollector-bot 里）。
"""

from __future__ import annotations

import json
import secrets
import time
from datetime import datetime
from typing import Any

from .config import get_settings


def now_ms() -> int:
    """当前时间（毫秒整数）。契约里所有时间戳都是这个形状。"""
    return int(time.time() * 1000)


def new_id(prefix: str = "") -> str:
    """时间有序的短 ID（前 13 位为毫秒时间戳），便于按创建顺序排序。

    附件用 `att_`、缺口告警用 `gap_` 前缀 —— 契约里的示例就是这个形状，
    也让日志里一眼能看出这是哪类对象。
    """
    return f"{prefix}{now_ms():013d}{secrets.token_hex(4)}"


def local_day(ts_ms: int | None = None) -> str:
    """返回**服务器本地时区**下的 YYYY-MM-DD。

    只用在两个地方：`POST /api/stats` 省略 day 时的默认值，以及
    group_state 的今日计数。bot 想用自己的时区时显式传 `day` 即可。
    """
    ts = now_ms() if ts_ms is None else ts_ms
    return datetime.fromtimestamp(ts / 1000).strftime("%Y-%m-%d")


def preview(text: Any, limit: int | None = None) -> str:
    """把文本压成一行、截断到 LOG_PREVIEW_CHARS，供日志使用。

    日志里打印整条消息或整个标题没有意义（一行几百字会把日志刷爆），
    但完全不打印又没法排障，所以长度可配。
    """
    if text is None:
        return ""
    size = get_settings().log_preview_chars if limit is None else limit
    text = str(text).replace("\r", " ").replace("\n", " / ")
    return text if len(text) <= size else text[:size] + "…"


def json_loads(value: Any, fallback: Any) -> Any:
    """库里存的是 JSON 文本；解析失败时给回退值而不是让整个接口 500。

    老库里可能存在手工改坏的 JSON，接口不该因此而挂掉。
    """
    if value is None or value == "":
        return fallback
    if isinstance(value, (list, dict)):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return fallback


def as_int(value: Any) -> int | None:
    """宽松地把外部输入转成毫秒整数；转不了就返回 None（由调用方决定报错与否）。"""
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None
