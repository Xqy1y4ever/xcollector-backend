"""手动建任务（QQ 里用 `/add <自由文本>` 指令）。

和群消息走**同一套解析**（规则 + LLM），区别只在入口：
  - 群消息：解析结果无条件入库，因为原文是客观事实
  - 手动任务：是用户自己打的字，解析没把握时应当**先回问再建**，
    否则会往任务列表里塞一条用户没打算要的东西

`needs_confirm` 的判定刻意保守：只要解析不出截止时间，或者置信度低于阈值，
就要求确认。多问一句的成本远低于建错一条任务。
"""

from __future__ import annotations

import logging

from ..config import get_settings
from ..db import (
    bump_stat,
    fetch_one,
    insert_raw_message,
    set_raw_state,
    upsert_notification,
)
from ..materialize import build_view
from ..utils import new_id, now_ms
from .extract import extract_with_llm
from .rule_extract import rule_extract
from .trace import log_message

logger = logging.getLogger(__name__)

MANUAL_GROUP_NAME = "手动添加"
MAX_TITLE = 60


def _synthetic_raw(text: str, sender_id: str, sender_name: str, ts: int) -> dict:
    """把手动输入包装成和群消息同构的 raw，好让抽取层完全复用。"""
    return {
        "message_id": f"manual-{new_id()}",
        "group_id": f"manual:{sender_id or 'unknown'}",
        "group_name": MANUAL_GROUP_NAME,
        "sender_id": sender_id or "unknown",
        "sender_name": sender_name or sender_id or "unknown",
        "ts": ts,
        "content": text,
        "attachments": [],
        "at_all": False,
    }


async def _parse(raw: dict, text: str) -> tuple[dict | None, bool]:
    """返回 (解析结果, 是否降级)。解析结果沿用 runner 的那套 dict 结构。"""
    settings = get_settings()
    rule_result = rule_extract(text, int(raw["ts"]), at_all=False)

    if settings.extractor == "rule":
        return rule_result, False

    try:
        out = await extract_with_llm(raw, [])
        result = out.get("result")
        if result is None:
            # 模型觉得这不是一条任务 —— 但用户是主动打的字，宁可保留规则的结果
            return rule_result, False
        return result, False
    except Exception as exc:
        # LLM 失败时降级到规则，并让调用方知道这次解析不够可靠
        logger.warning("手动任务的 LLM 解析失败，降级为规则：%s", exc)
        return rule_result, True


def _preview(result: dict | None, text: str) -> dict:
    if result is None:
        return {
            "title": text[:MAX_TITLE],
            "summary": None,
            "location": None,
            "due_at": None,
            "due_text": None,
            "due_confidence": 0.0,
        }
    return {
        "title": result.get("title") or text[:MAX_TITLE],
        "summary": result.get("summary"),
        "location": result.get("location"),
        "due_at": result.get("due_at"),
        "due_text": result.get("due_text"),
        "due_confidence": float(result.get("due_confidence") or 0.0),
    }


async def create_manual_task(
    *,
    text: str,
    sender_id: str = "",
    sender_name: str = "",
    auto_commit: bool = True,
    force_commit: bool = False,
) -> dict:
    settings = get_settings()
    text = text.strip()
    ts = now_ms()
    raw = _synthetic_raw(text, sender_id, sender_name, ts)

    result, degraded = await _parse(raw, text)
    preview = _preview(result, text)

    # ---- 判断要不要先回问 ----
    reason: str | None = None
    if result is None:
        reason = "没能识别出这是一条任务"
    elif result.get("due_at") is None:
        reason = "没能解析出明确的截止时间"
    elif float(result.get("due_confidence") or 0.0) < settings.low_confidence_threshold:
        reason = "截止时间的把握不大"
    elif degraded:
        reason = "模型不可用，这次只用规则解析"

    needs_confirm = reason is not None

    if needs_confirm and not force_commit:
        log_message(
            {
                "group_id": raw["group_id"],
                "group_name": MANUAL_GROUP_NAME,
                "sender_id": sender_id,
                "sender_name": sender_name,
                "message_id": raw["message_id"],
                "ts": ts,
                "content": text,
            },
            "needs_confirm",
            原因=reason,
        )
        return {
            "ok": True,
            "needs_confirm": True,
            "reason": reason,
            "preview": preview,
            "task": None,
        }

    # ---- 建条 ----
    evidence = (result or {}).get("evidence") or text[:200]
    title = preview["title"] or text[:MAX_TITLE]

    raw_id, _ = await insert_raw_message(
        message_id=raw["message_id"],
        group_id=raw["group_id"],
        group_name=MANUAL_GROUP_NAME,
        sender_id=raw["sender_id"],
        sender_name=raw["sender_name"],
        ts=ts,
        content=text,
        attachments=[],
        raw={**raw, "manual": True, "confirmed": bool(force_commit)},
    )

    await upsert_notification(
        {
            "raw_message_id": raw_id,
            "group_id": raw["group_id"],
            "group_name": MANUAL_GROUP_NAME,
            "sender_id": raw["sender_id"],
            "sender_name": raw["sender_name"],
            "source_ts": ts,
            "title": title,
            "summary": preview["summary"],
            "location": preview["location"],
            "due_at": preview["due_at"],
            "due_text": preview["due_text"],
            "due_confidence": preview["due_confidence"],
            "evidence": evidence,
            "conflict": bool((result or {}).get("conflict")),
            "candidates": (result or {}).get("candidates") or [],
            "extractor": (result or {}).get("extractor") or "manual",
            "model": (result or {}).get("model"),
            "prompt_ver": (result or {}).get("prompt_ver"),
        }
    )
    await set_raw_state(raw_id, "extracted")
    await bump_stat("extracted")

    row = await fetch_one(
        """SELECT n.*, r.attachments FROM notification n
           LEFT JOIN raw_message r ON r.id = n.raw_message_id
           WHERE n.raw_message_id = ?""",
        (raw_id,),
    )
    view = await build_view(row) if row else None
    if view:
        view.pop("_read", None)

    log_message(
        {
            "group_id": raw["group_id"],
            "group_name": MANUAL_GROUP_NAME,
            "sender_id": sender_id,
            "sender_name": sender_name,
            "message_id": raw["message_id"],
            "ts": ts,
            "content": text,
        },
        "created",
        标题=title,
        截止=preview["due_text"],
        地点=preview["location"],
    )

    return {
        "ok": True,
        "needs_confirm": False,
        "reason": reason,
        "preview": preview,
        "task": view,
    }
