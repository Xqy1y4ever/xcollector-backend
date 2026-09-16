"""把库里的行变成 API 视图。

核心逻辑：**correction 表覆盖 notification 表**。
这样重跑抽取（改 prompt、换模型）永远不会覆盖人工修正过的字段。
"""

from __future__ import annotations

import json

from .db import corrections_for, read_map
from .utils import now_ms


def _correction_to_value(field: str, raw_value):
    if raw_value is None:
        return None
    if field == "due_at":
        try:
            return int(float(raw_value))
        except (TypeError, ValueError):
            return None
    return raw_value


def _effective(row: dict, corr: dict) -> dict:
    due_at = row.get("due_at")
    due_text = row.get("due_text")
    title = row.get("title")
    summary = row.get("summary")

    if "due_at" in corr:
        due_at = _correction_to_value("due_at", corr["due_at"])
    if "due_text" in corr:
        due_text = corr["due_text"]
    if "title" in corr and corr["title"]:
        title = corr["title"]
    if "summary" in corr:
        summary = corr["summary"]

    if "status" in corr and corr["status"]:
        status = corr["status"]
    elif due_at is not None and due_at < now_ms():
        status = "expired"
    else:
        status = "active"

    try:
        candidates = json.loads(row.get("candidates") or "[]")
    except json.JSONDecodeError:
        candidates = []
    try:
        attachments = json.loads(row.get("attachments") or "[]")
    except json.JSONDecodeError:
        attachments = []

    return {
        "id": row["id"],
        "group_id": row.get("group_id"),
        "group_name": row.get("group_name"),
        "sender_id": row.get("sender_id"),
        "sender_name": row.get("sender_name"),
        "title": title,
        "summary": summary,
        "due_at": due_at,
        "due_text": due_text,
        "due_confidence": row.get("due_confidence") or 0.0,
        "conflict": bool(row.get("conflict")),
        "candidates": candidates,
        "evidence": row.get("evidence") or "",
        "status": status,
        "manually_edited": bool(corr),
        "read": bool(row.get("_read")),
        "attachments": attachments,
        "extractor": row.get("extractor"),
        "model": row.get("model"),
        "prompt_ver": row.get("prompt_ver"),
        "source_ts": row.get("source_ts"),
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
    }


async def build_views(rows: list[dict]) -> list[dict]:
    if not rows:
        return []
    corr_map = await corrections_for([r["id"] for r in rows])
    reads = await read_map()
    out = []
    for row in rows:
        r = dict(row)
        r["_read"] = r["id"] in reads
        out.append(_effective(r, corr_map.get(r["id"], {})))
    return out


async def build_view(row: dict) -> dict:
    views = await build_views([row])
    return views[0]
