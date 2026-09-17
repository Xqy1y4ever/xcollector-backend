"""附件存储。

职责只有三件事：
  1. 解析 `multipart/form-data`（两个文本字段 + 一个文件）
  2. 字节落盘到 `ATTACHMENT_DIR`，库里只写元数据
  3. 读回时把正确的 `Content-Type` / `Content-Disposition` 还回去

为什么自己解析 multipart：契约里这个接口只有两个文本字段加一个文件，
而本仓库的依赖里没有 `python-multipart`（FastAPI 的 `UploadFile` 依赖它）。
标准 multipart 的解析不到 50 行就能覆盖，比多一个"必须装上才能上传"的
依赖更稳。

目录穿越在这里**结构上不可能**：实际落盘文件名由 id 生成，路径里不含任何
外部传入的字符串；外部 filename 只清洗后留档（显示用）。
"""

from __future__ import annotations

import asyncio
import logging
import mimetypes
import re
import urllib.parse
from dataclasses import dataclass

from fastapi import Request

from .config import get_settings
from .db import get_attachment, insert_attachment
from .signing import attachment_path
from .utils import new_id

logger = logging.getLogger(__name__)

# multipart 的头部字段（boundary、name、filename、source_url）留的余量：
# 超过 MEDIA_MAX_BYTES + 这个值就直接拒，不把大包读进内存
MULTIPART_SLACK = 64 * 1024

# 落盘扩展名的白名单形状：只允许 ".jpg" 这种短小的字母数字后缀
_EXT_RE = re.compile(r"\.[A-Za-z0-9]{1,12}$")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_DISPOSITION_PARAM_RE = re.compile(r'([a-zA-Z0-9_*]+)\s*=\s*"((?:[^"\\]|\\.)*)"')
_BOUNDARY_RE = re.compile(r'boundary="?([^";,]+)"?', re.IGNORECASE)

# 这些类型可以在浏览器里直接内联显示（前端要用 <img> 直接指向 /api/attachments/xxx）。
# 其余一律 attachment 下载，并配上 nosniff —— 附件内容来自外部，不该被当成页面执行。
_INLINE_PREFIXES = ("image/", "video/", "audio/", "text/plain", "application/pdf")


class MultipartError(ValueError):
    """请求体不是能识别的 multipart/form-data。"""


class TooLarge(ValueError):
    """附件超过 MEDIA_MAX_BYTES（对应 HTTP 413）。"""


@dataclass
class Part:
    name: str
    filename: str | None
    content_type: str | None
    data: bytes


# --------------------------------------------------------------------------
# 文件名清洗 / 响应头
# --------------------------------------------------------------------------


def safe_filename(name: str | None) -> str | None:
    """把外部传来的文件名压成一个**不含任何路径成分**的纯名字。

    `../../evil.txt` → `evil.txt`；`..\\..\\evil` → `evil`；纯 `.` / `..` → None。
    它只用于留档与下载时的显示名 —— 真正落盘的名字由 id 生成。
    """
    if not name:
        return None
    cleaned = _CONTROL_RE.sub("", str(name)).replace("\\", "/").split("/")[-1].strip()
    if cleaned in ("", ".", ".."):
        return None
    return cleaned[:200]


def guess_content_type(filename: str | None, declared: str | None = None) -> str:
    declared = (declared or "").split(";")[0].strip()
    if declared:
        return declared
    guessed, _ = mimetypes.guess_type(filename or "")
    return guessed or "application/octet-stream"


def content_disposition(filename: str | None, content_type: str) -> str:
    """按 RFC 6266 生成 Content-Disposition（中文文件名要 filename* 才不炸）。"""
    kind = "inline" if content_type.startswith(_INLINE_PREFIXES) else "attachment"
    name = filename or "attachment"
    fallback = "".join(
        ch if 32 <= ord(ch) < 127 and ch not in '"\\' else "_" for ch in name
    ) or "attachment"
    quoted = urllib.parse.quote(name, safe="")
    return f"{kind}; filename=\"{fallback}\"; filename*=UTF-8''{quoted}"


# --------------------------------------------------------------------------
# multipart 解析
# --------------------------------------------------------------------------


def _decode(value: bytes) -> str:
    return value.decode("utf-8", "replace")


def _parse_disposition(value: str) -> dict[str, str]:
    params: dict[str, str] = {}
    for key, raw in _DISPOSITION_PARAM_RE.findall(value):
        params[key.lower()] = re.sub(r"\\(.)", r"\1", raw)
    # filename*=UTF-8''%E4%B8%AD%E6%96%87.jpg 形式（httpx 对非 ASCII 用它）
    match = re.search(r"filename\*\s*=\s*([^;]+)", value, re.IGNORECASE)
    if match and "filename" not in params:
        raw = match.group(1).strip().strip('"')
        charset, sep, encoded = raw.partition("''")
        if sep:
            params["filename"] = urllib.parse.unquote(
                encoded, encoding=charset or "utf-8", errors="replace"
            )
    return params


def _trim_edges(chunk: bytes) -> bytes:
    """去掉分片两端各一个 CRLF（它们是分隔符的一部分，不属于数据）。"""
    for edge in (b"\r\n", b"\n"):
        if chunk.startswith(edge):
            chunk = chunk[len(edge):]
            break
    for edge in (b"\r\n", b"\n"):
        if chunk.endswith(edge):
            chunk = chunk[: -len(edge)]
            break
    return chunk


def parse_multipart(body: bytes, content_type: str) -> list[Part]:
    match = _BOUNDARY_RE.search(content_type or "")
    if not match:
        raise MultipartError("multipart 缺少 boundary")
    delimiter = b"--" + match.group(1).strip().encode("utf-8")

    parts: list[Part] = []
    for chunk in body.split(delimiter)[1:]:
        if chunk.startswith(b"--"):  # 结束分隔符
            break
        chunk = _trim_edges(chunk)
        if not chunk:
            continue
        head, sep, data = chunk.partition(b"\r\n\r\n")
        if not sep:
            head, sep, data = chunk.partition(b"\n\n")
            if not sep:
                continue

        headers: dict[str, str] = {}
        for line in head.replace(b"\r\n", b"\n").split(b"\n"):
            if b":" not in line:
                continue
            key, _, value = line.partition(b":")
            headers[_decode(key).strip().lower()] = _decode(value).strip()

        disposition = _parse_disposition(headers.get("content-disposition", ""))
        parts.append(
            Part(
                name=disposition.get("name", ""),
                filename=disposition.get("filename"),
                content_type=headers.get("content-type"),
                data=data,
            )
        )
    if not parts:
        raise MultipartError("multipart 里没有解析出任何字段")
    return parts


async def read_multipart(request: Request, *, max_bytes: int) -> list[Part]:
    """按流读取请求体，超限立刻中止（不把整个大包读进内存）。"""
    content_type = request.headers.get("content-type", "")
    if "multipart/form-data" not in content_type.lower():
        raise MultipartError("Content-Type 必须是 multipart/form-data")

    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > max_bytes + MULTIPART_SLACK:
        raise TooLarge(f"附件超过上限 {max_bytes} 字节")

    buffer = bytearray()
    async for chunk in request.stream():
        buffer += chunk
        if len(buffer) > max_bytes + MULTIPART_SLACK:
            raise TooLarge(f"附件超过上限 {max_bytes} 字节")
    return parse_multipart(bytes(buffer), content_type)


# --------------------------------------------------------------------------
# 落盘 / 读回
# --------------------------------------------------------------------------


async def handle_upload(request: Request) -> dict:
    """`POST /api/attachments` 的全部逻辑：收 → 校验大小 → 落盘 → 记元数据。"""
    settings = get_settings()
    parts = await read_multipart(request, max_bytes=settings.media_max_bytes)

    file_part = next((p for p in parts if p.name == "file"), None)
    if file_part is None:
        raise MultipartError("缺少 file 字段")

    fields = {p.name: _decode(p.data).strip() for p in parts if p is not file_part}
    filename = fields.get("filename") or file_part.filename
    source_url = fields.get("source_url") or None

    return await store_upload(
        filename=filename,
        data=file_part.data,
        content_type=file_part.content_type,
        source_url=source_url,
    )


async def store_upload(
    *,
    filename: str | None,
    data: bytes,
    content_type: str | None = None,
    source_url: str | None = None,
) -> dict:
    settings = get_settings()
    limit = settings.media_max_bytes
    if len(data) > limit:
        raise TooLarge(f"附件超过上限 {limit} 字节")

    directory = settings.resolved_attachment_dir
    directory.mkdir(parents=True, exist_ok=True)

    att_id = new_id("att_")
    clean = safe_filename(filename)
    suffix = ""
    if clean:
        match = _EXT_RE.search(clean)
        if match:
            suffix = match.group(0).lower()
    stored_name = f"{att_id}{suffix}"

    path = (directory / stored_name).resolve()
    if path.parent != directory.resolve():  # 结构上不该发生，留一道闸
        raise ValueError("附件路径越界")

    resolved_type = guess_content_type(clean, content_type)
    await asyncio.to_thread(path.write_bytes, data)
    try:
        await insert_attachment(
            att_id=att_id,
            filename=clean,
            stored_name=stored_name,
            content_type=resolved_type,
            size=len(data),
            source_url=(source_url or None) and str(source_url)[:2000],
        )
    except Exception:
        path.unlink(missing_ok=True)
        raise

    logger.info(
        "附件已入库：id=%s 大小=%d 类型=%s 文件=%s 来源=%s",
        att_id,
        len(data),
        resolved_type,
        clean or "(无文件名)",
        (source_url or "(无)")[:80],
    )
    return {
        "id": att_id,
        # **裸路径**：这是规范引用，bot 会把它存进 raw_message.attachments。
        # 真正对外提供的 URL 不在这里签 —— 签名会过期，存下来历史条目就打不开了。
        # 读投影（materialize.to_view / raw_view）每次读取时现签，
        # 见 signing.sign_attachments。
        "url": attachment_path(att_id),
        "size": len(data),
        "content_type": resolved_type,
    }


async def load_bytes(att_id: str) -> tuple[dict, bytes] | None:
    """读回附件行的元数据与二进制；行不存在或文件丢了都返回 None。"""
    row = await get_attachment(att_id)
    if row is None:
        return None
    path = get_settings().resolved_attachment_dir / str(row["stored_name"])
    try:
        data = await asyncio.to_thread(path.read_bytes)
    except OSError:
        logger.warning("附件行存在但文件读不到：id=%s 路径=%s", att_id, path)
        return None
    return row, data
