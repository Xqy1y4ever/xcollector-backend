"""附件签名 URL。

**为什么需要它**：前端用 `<img src>` / `<a href>` 取附件，而浏览器这两个标签
**带不了 `Authorization` 头**。所以在"所有 /api 都要 Bearer"的规则下，证据图和
附件下载永远 401。

解法是给附件单独一条路：读投影里的 `url` **每次现签**，带上过期时间和 HMAC：

    /api/attachments/att_cf5529?exp=1780000000&sig=9f3c...

下载接口接受"有效签名"**或**"有效 Bearer 令牌"，两者任一即可（见 auth.py）。

两个刻意的决定：

  - **签名在读取时做，不入库。** 上传响应和库里存的是**裸路径**（规范引用），
    否则存下来的签名会过期，历史条目的图全部打不开。
  - **数据库里的 `url` 每次读取都被覆盖成新签的**，所以前端不需要做任何事 ——
    它本来就读 `attachment.url`。

密钥来源：`ATTACHMENT_SIGN_KEY`，留空则从 `API_TOKEN` 派生。两者都没有
（= 未启用认证的本地开发）时不签名，退回"必须带 Bearer"的裸路径。
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from urllib.parse import quote

from .config import get_settings

# 签名长度。32 个 hex 字符 = 128 bit，对"短时效 URL"足够，且 URL 不至于太长。
_SIG_LEN = 32

# 派生密钥时的上下文串：同一个 API_TOKEN 将来若还用于别的签名用途，
# 两边的密钥不会撞在一起。
_CONTEXT = b"xcollector-attachment-url-v1"


def _key() -> bytes:
    """签名密钥。没有可用密钥时返回空 bytes（= 不签名）。"""
    settings = get_settings()
    secret = (settings.attachment_sign_key or "").strip() or (settings.api_token or "").strip()
    if not secret:
        return b""
    return hmac.new(_CONTEXT, secret.encode("utf-8"), hashlib.sha256).digest()


def _signature(key: bytes, att_id: str, exp: int) -> str:
    return hmac.new(key, f"{att_id}.{exp}".encode("utf-8"), hashlib.sha256).hexdigest()[:_SIG_LEN]


def attachment_path(att_id: str) -> str:
    """附件的规范路径（不带签名）。入库、上传响应用的就是它。"""
    return f"/api/attachments/{quote(str(att_id), safe='')}"


def sign_attachment_url(att_id: str, *, now: int | None = None) -> str:
    """签发一个带过期时间的附件 URL。

    没配密钥或 TTL<=0 时返回裸路径 —— 那种情况下下载接口仍然接受 Bearer。
    """
    base = attachment_path(att_id)
    key = _key()
    try:
        ttl = int(get_settings().attachment_url_ttl or 0)
    except (TypeError, ValueError):
        ttl = 0
    if not key or ttl <= 0:
        return base
    exp = int(now if now is not None else time.time()) + ttl
    return f"{base}?exp={exp}&sig={_signature(key, str(att_id), exp)}"


def verify_attachment_sig(
    att_id: str,
    exp: object,
    sig: object,
    *,
    now: int | None = None,
) -> bool:
    """校验签名与过期时间。任何异常都返回 False（**失败即拒绝**）。"""
    key = _key()
    if not key:
        return False
    try:
        exp_i = int(str(exp))
    except (TypeError, ValueError):
        return False
    if exp_i < int(now if now is not None else time.time()):
        return False
    # 定长比较，避免通过响应时间逐字节猜签名
    return secrets.compare_digest(_signature(key, str(att_id), exp_i), str(sig or ""))


def _attachment_id_of(item: dict) -> str:
    """从附件 dict 里取 id。没有 id 时退回从 url 里抠 —— 兼容老数据。"""
    att_id = item.get("id")
    if att_id:
        return str(att_id)
    url = item.get("url")
    if isinstance(url, str) and "/attachments/" in url:
        tail = url.split("/attachments/", 1)[1]
        return tail.split("?", 1)[0].split("/", 1)[0]
    return ""


def sign_attachments(items: object) -> list[dict]:
    """把附件列表里的 `url` 换成现签的签名 URL。非列表/非 dict 原样保留。"""
    if not isinstance(items, list):
        return []
    out: list[dict] = []
    for item in items:
        if not isinstance(item, dict):
            out.append(item)
            continue
        att_id = _attachment_id_of(item)
        if not att_id:
            out.append(dict(item))
            continue
        # 覆盖式写 url：库里存的裸路径在这里被换成带 exp+sig 的版本
        out.append({**item, "url": sign_attachment_url(att_id)})
    return out
