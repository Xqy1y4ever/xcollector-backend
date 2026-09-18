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


def _signature(key: bytes, user_id: str, att_id: str, exp: int) -> str:
    # user_id 进签名内容：**这是多用户下最关键的一行**。
    # 不绑的话，拿到别人通知里那条链接的人就能看别人的附件 ——
    # 而那条链接本身看起来完全正常。
    return hmac.new(
        key, f"{user_id}.{att_id}.{exp}".encode("utf-8"), hashlib.sha256
    ).hexdigest()[:_SIG_LEN]


def attachment_path(att_id: str) -> str:
    """附件的规范路径（不带签名）。入库、上传响应用的就是它。"""
    return f"/api/attachments/{quote(str(att_id), safe='')}"


def sign_attachment_url(user_id: str, att_id: str, *, now: int | None = None) -> str:
    """签发一个带过期时间、且**只对某个用户有效**的附件 URL。

    没有可用密钥 / TTL<=0 / **没有归属** 时都返回裸路径 —— 那几种情况下
    下载接口仍然接受 Bearer，而 Bearer 是有身份的，所以不会因此串数据。

    「没有归属」这一条是必须的，不是省事：`GET /api/messages*` 是共享层，
    它没有 owner 可以绑。以前这里会签出一条 `?exp=..&u=&sig=..` 的链接，
    而校验侧要求 `u` 非空 —— **那条链接永远 401**，而且看起来完全正常
    （有 exp 有 sig）。宁可返回裸路径：它诚实地说"我得靠 Bearer"，
    而不是伪造一个用不了的签名。
    """
    base = attachment_path(att_id)
    owner = str(user_id or "").strip()
    key = _key()
    if not owner or not key:
        return base
    try:
        ttl = int(get_settings().attachment_url_ttl or 0)
    except (TypeError, ValueError):
        ttl = 0
    if ttl <= 0:
        return base
    exp = int(now if now is not None else time.time()) + ttl
    # `u=` 必须带上：下载请求**没有 Authorization 头**（浏览器 <img> 带不了），
    # 验证方只能从 URL 里知道"这条链接是给谁的"。它进了签名内容，改不动。
    return (
        f"{base}?exp={exp}&u={quote(owner, safe='')}"
        f"&sig={_signature(key, owner, str(att_id), exp)}"
    )


def verify_attachment_sig(
    user_id: str,
    att_id: str,
    exp: object,
    sig: object,
    *,
    now: int | None = None,
) -> bool:
    """校验签名、归属与过期时间。任何异常都返回 False（**失败即拒绝**）。"""
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
    return secrets.compare_digest(
        _signature(key, str(user_id), str(att_id), exp_i), str(sig or "")
    )


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


def sign_attachments(user_id: str, items: object) -> list[dict]:
    """把附件列表里的 `url` 换成现签的、**绑定该用户**的签名 URL。"""
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
        out.append({**item, "url": sign_attachment_url(user_id, att_id)})
    return out
