"""抽取编排：把一条原始消息变成一条（或零条）通知。

三条原则在这里落地：
  - **绝不静默丢弃**：任何异常都会把 raw 标成 unparsed / degraded，原文始终在库里
  - **降级要留痕**：LLM 整体失败时用规则兜底，并记 degraded 统计
  - **分歧要暴露**：模型与规则、模型与模型之间的不一致，一律标 conflict 让人确认
"""

from __future__ import annotations

import base64
import json
import logging
import mimetypes

from ..config import get_settings
from ..db import bump_stat, get_raw, set_raw_state, upsert_notification
from .extract import extract_with_llm
from .rule_extract import rule_extract
from .trace import describe_attachment_count, fmt_due, log_message

logger = logging.getLogger(__name__)


def _image_data_urls(attachments: list[dict]) -> list[str]:
    """把已落地的图片转成 data URL，供多模态模型读取。

    官方通知经常把 DDL 写在图片里 —— 不处理图片，这里就是最大的漏信息来源。
    """
    settings = get_settings()
    if not settings.vlm_enabled:
        return []
    root = settings.resolved_attachment_dir.parent.parent
    urls: list[str] = []
    for att in attachments:
        if att.get("type") != "image" or not att.get("local_path"):
            continue
        path = root / att["local_path"]
        if not path.exists():
            continue
        mime = mimetypes.guess_type(path.name)[0] or "image/jpeg"
        try:
            b64 = base64.b64encode(path.read_bytes()).decode("ascii")
        except Exception as exc:
            logger.warning("读取图片失败 %s: %s", path, exc)
            continue
        urls.append(f"data:{mime};base64,{b64}")
        if len(urls) >= settings.vlm_max_images:
            break
    return urls


def _merge_rule_disagreement(llm_result: dict | None, rule_result: dict | None, model: str) -> dict | None:
    """模型说"不是通知"，但规则认为有明确时间 → 保留条目并标冲突。

    方向是刻意的：宁可多推一条让人一键否决，也不能漏掉一条真通知。
    """
    if llm_result is not None:
        return llm_result
    if rule_result is None:
        return None

    merged = dict(rule_result)
    merged["conflict"] = True
    merged["due_confidence"] = min(float(merged.get("due_confidence") or 0), 0.5)
    merged["candidates"] = [
        {"model": "rule-engine", "due_at": merged.get("due_at"), "due_text": merged.get("due_text")},
        {"model": model, "due_at": None, "due_text": None, "note": "模型判定为非通知"},
    ]
    return merged


async def process_raw(raw_id: str) -> None:
    raw = await get_raw(raw_id)
    if raw is None:
        return
    if raw.get("state") != "pending":
        return  # 已处理过，保证幂等

    settings = get_settings()
    try:
        attachments = json.loads(raw.get("attachments") or "[]")
    except json.JSONDecodeError:
        attachments = []

    content = raw.get("content") or ""
    rule_result = rule_extract(content, int(raw["ts"]), at_all="@全体成员" in content)

    result: dict | None = None
    degraded = False
    tokens = 0

    if settings.extractor in ("llm", "both"):
        images = _image_data_urls(attachments)
        try:
            out = await extract_with_llm(dict(raw), images)
            tokens = int(out.get("tokens") or 0)
            result = _merge_rule_disagreement(
                out.get("result"), rule_result, settings.llm_primary_model
            )
        except Exception as exc:
            # LLM 这条路整体失败：降级到规则，并把这件事记下来
            degraded = True
            logger.error("LLM 抽取失败，降级为规则抽取 raw=%s: %s", raw_id, exc)
            result = rule_result
    else:
        result = rule_result

    if tokens:
        await bump_stat("llm_tokens", tokens)

    if result is None:
        if degraded:
            # LLM 失败、规则也没兜住 —— 这是真的盲区
            await set_raw_state(raw_id, "degraded", "LLM 失败且规则也无法解析")
            await bump_stat("degraded")
            await bump_stat("unparsed")
            log_message(raw, "degraded", 原因="LLM 失败且规则也无法解析", 抽取器=settings.extractor)
        else:
            # 判定为闲聊/回执，属于正常结果，不该计入"未能解析"
            await set_raw_state(raw_id, "noise", "判定为非通知")
            log_message(raw, "noise", 抽取器=settings.extractor)
        return

    # 硬约束：evidence 必须非空。没有证据的条目宁可不要。
    evidence = (result.get("evidence") or "").strip()
    if not evidence:
        await set_raw_state(raw_id, "unparsed", "抽取结果缺少 evidence，已拒绝建条")
        await bump_stat("unparsed")
        log_message(
            raw,
            "unparsed",
            原因="抽取结果缺少 evidence，已拒绝建条",
            抽取器=settings.extractor,
        )
        return

    payload = {
        "raw_message_id": raw_id,
        "group_id": raw["group_id"],
        "group_name": raw.get("group_name"),
        "sender_id": raw.get("sender_id"),
        "sender_name": raw.get("sender_name"),
        "source_ts": int(raw["ts"]),
        "title": result["title"],
        "summary": result.get("summary"),
        "location": result.get("location"),
        "due_at": result.get("due_at"),
        "due_text": result.get("due_text"),
        "due_confidence": float(result.get("due_confidence") or 0.0),
        "evidence": evidence,
        "conflict": bool(result.get("conflict")),
        "candidates": result.get("candidates") or [],
        "extractor": result.get("extractor") or settings.extractor,
        "model": result.get("model"),
        "prompt_ver": result.get("prompt_ver"),
    }
    await upsert_notification(payload)
    await set_raw_state(raw_id, "extracted")
    await bump_stat("extracted")
    if degraded:
        await bump_stat("degraded")

    log_message(
        raw,
        "extracted",
        标题=payload["title"],
        截止=fmt_due(payload["due_at"], payload["due_text"]),
        地点=payload.get("location"),
        置信度=payload["due_confidence"],
        冲突="是" if payload["conflict"] else None,
        抽取器=payload["extractor"],
        模型=payload["model"],
        tokens=tokens or None,
        附件=describe_attachment_count(raw) or None,
    )
