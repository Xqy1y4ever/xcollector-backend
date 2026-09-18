"""用户、邀请码、QQ 验证码。

多用户服务的地基，三件事分开：

  1. **注册**：证明「这个 QQ 号确实是你的」—— 用户在 QQ 上向 bot 索取验证码，
     bot 转手把码发回给他（`app/main.py` 的那条私聊路径）。**机器人发不出陌生人
     私聊**（不是好友、也没有共同群临时会话时 QQ 会直接拒），所以方向只能是
     「用户先找机器人」，不能是「网页填个 QQ 机器人去发」。
  2. **邀请码**：控制谁能注册。默认 `SIGNUP_MODE=invite` —— 每次注册都要消耗
     一个邀请码，运营者用 `API_TOKEN` 通过 `/api/invites` 发码。
  3. **令牌**：注册成功后发一个 `UserToken`，它同时是登录凭证和一切后端调用的
     凭证。**库里只存 sha256**，所以令牌只在注册/重置那一次显示，拿不回来第二次
     （和 API key 一个道理，库被拿走不等于所有人的令牌被拿走）。

关于"找回令牌"：没有邮箱、没有密保，能证明身份的还是那个 QQ 号。所以
**同一套流程直接复用**：已有用户再次走「QQ 要码 → 网页提交」，结果是**轮换令牌**
而不是报「已注册」—— 否则令牌一丢账号就废了。轮换不需要邀请码（他已经是用户了）。
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import re
import secrets
from typing import Any

from .config import get_settings
from .db import execute, fetch_all, fetch_one
from .utils import new_id, now_ms

logger = logging.getLogger(__name__)

# QQ 号：5~12 位数字。放宽到 5 位是为了兼容很早的号，收紧上限是防呆。
_QQ_RE = re.compile(r"^[1-9]\d{4,11}$")

# 令牌前缀：一是让用户一眼认出这是什么，二是万一被贴进日志/issue 里好 grep。
TOKEN_PREFIX = "xc_"


class UserError(Exception):
    """用户流程里可以**直接讲给用户听**的错误。

    detail 会原样返回给调用方，所以只写"他该怎么办"，不要写内部细节。
    """

    def __init__(self, detail: str, *, status_code: int = 400):
        super().__init__(detail)
        self.detail = detail
        self.status_code = status_code


# --------------------------------------------------------------------------
# 校验与生成
# --------------------------------------------------------------------------


def normalize_qq(value: Any) -> str:
    """把 QQ 号规整成字符串；不合法就抛 UserError（消息可直接展示）。"""
    qq = "" if value is None else str(value).strip()
    if not qq:
        raise UserError("请填写 QQ 号")
    if not _QQ_RE.match(qq):
        raise UserError("QQ 号看起来不对：应该是 5~12 位数字，且不以 0 开头")
    return qq


def new_token() -> str:
    return TOKEN_PREFIX + secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.strip().encode("utf-8")).hexdigest()


def token_hint(token: str) -> str:
    return token[: len(TOKEN_PREFIX) + 6]


def new_verify_code() -> str:
    """6 位数字，均匀分布（`randbelow` 没有取模偏置）。"""
    return f"{secrets.randbelow(1_000_000):06d}"


# --------------------------------------------------------------------------
# 邀请码
# --------------------------------------------------------------------------


async def create_invite(
    *,
    note: str | None = None,
    max_uses: int = 1,
    ttl_seconds: int | None = None,
) -> dict:
    code = new_id("inv")
    now = now_ms()
    expires_at = now + ttl_seconds * 1000 if ttl_seconds else None
    await execute(
        "INSERT INTO invite_code (code, note, max_uses, used_count, expires_at, created_at)"
        " VALUES (?, ?, ?, 0, ?, ?)",
        (code, note, max(1, int(max_uses)), expires_at, now),
    )
    return {
        "code": code,
        "note": note,
        "max_uses": max(1, int(max_uses)),
        "used_count": 0,
        "expires_at": expires_at,
        "created_at": now,
    }


async def list_invites() -> list[dict]:
    return await fetch_all("SELECT * FROM invite_code ORDER BY created_at DESC")


async def _consume_invite(code: str) -> None:
    """校验并消耗一个邀请码；不通过就抛 UserError。"""
    normalized = (code or "").strip()
    if not normalized:
        raise UserError("需要邀请码才能注册。请向服务提供者索取。")

    row = await fetch_one("SELECT * FROM invite_code WHERE code = ?", (normalized,))
    if row is None:
        raise UserError("邀请码无效")
    if row["expires_at"] is not None and int(row["expires_at"]) < now_ms():
        raise UserError("邀请码已过期")
    if int(row["used_count"]) >= int(row["max_uses"]):
        raise UserError("邀请码已用完")

    # 条件更新：并发注册同一个码时只有一个能把 used_count 推上去
    changed = await execute(
        "UPDATE invite_code SET used_count = used_count + 1"
        " WHERE code = ? AND used_count < max_uses",
        (normalized,),
    )
    if not changed:
        raise UserError("邀请码已用完")


# --------------------------------------------------------------------------
# QQ 验证码
# --------------------------------------------------------------------------


async def issue_verify_code(qq: str) -> dict:
    """给某个 QQ 生成验证码（覆盖旧的那个）。

    **只有 bot 能调**：否则任何人只要知道别人的 QQ 就能一直刷新他的验证码。
    """
    qq = normalize_qq(qq)
    settings = get_settings()
    code = new_verify_code()
    now = now_ms()
    expires_at = now + int(settings.verify_code_ttl) * 1000

    await execute(
        "INSERT INTO qq_verify_code (qq, code, expires_at, attempts, created_at)"
        " VALUES (?, ?, ?, 0, ?)"
        " ON CONFLICT(qq) DO UPDATE SET"
        "   code = excluded.code, expires_at = excluded.expires_at,"
        "   attempts = 0, created_at = excluded.created_at",
        (qq, code, expires_at, now),
    )
    logger.info("已为用户验证生成验证码 qq=%s（%d 秒内有效）", qq, settings.verify_code_ttl)
    return {"qq": qq, "code": code, "expires_at": expires_at, "ttl": settings.verify_code_ttl}


async def verify_code_matches(qq: str, code: str) -> bool:
    """校验验证码。**成功即作废**（一次一用），失败累计尝试次数，超限作废。

    任何异常路径都返回 False —— 这是认证入口，失败即拒绝。
    """
    settings = get_settings()
    row = await fetch_one("SELECT * FROM qq_verify_code WHERE qq = ?", (qq,))
    if row is None:
        return False

    if int(row["expires_at"]) < now_ms():
        await execute("DELETE FROM qq_verify_code WHERE qq = ?", (qq,))
        return False

    max_attempts = max(1, int(settings.verify_max_attempts))
    if int(row["attempts"]) >= max_attempts:
        await execute("DELETE FROM qq_verify_code WHERE qq = ?", (qq,))
        return False

    # 定长比较：不让人从响应时间上逐位猜
    if not hmac.compare_digest(str(row["code"]), (code or "").strip()):
        await execute(
            "UPDATE qq_verify_code SET attempts = attempts + 1 WHERE qq = ?", (qq,)
        )
        return False

    await execute("DELETE FROM qq_verify_code WHERE qq = ?", (qq,))
    return True


# --------------------------------------------------------------------------
# 用户
# --------------------------------------------------------------------------


def public_user(row: dict | None) -> dict | None:
    """对外的用户形状。**绝不包含 token_hash。**"""
    if row is None:
        return None
    return {
        "id": row.get("id"),
        "qq": row.get("qq"),
        "display_name": row.get("display_name"),
        "token_hint": row.get("token_hint"),
        "status": row.get("status"),
        "created_at": row.get("created_at"),
        "last_seen_at": row.get("last_seen_at"),
    }


async def get_user_by_qq(qq: str) -> dict | None:
    return await fetch_one("SELECT * FROM app_user WHERE qq = ?", (qq,))


async def get_user_by_id(user_id: str) -> dict | None:
    return await fetch_one("SELECT * FROM app_user WHERE id = ?", (user_id,))


async def get_user_by_token(token: str) -> dict | None:
    """按令牌找用户。**只比对摘要**，明文不入库、也不入日志。"""
    prepared = (token or "").strip()
    if not prepared.startswith(TOKEN_PREFIX):
        return None
    row = await fetch_one("SELECT * FROM app_user WHERE token_hash = ?", (hash_token(prepared),))
    if row is None:
        return None
    if row.get("status") != "active":
        return None
    return row


async def register_or_rotate(
    *,
    qq: str,
    code: str,
    invite_code: str | None = None,
    display_name: str | None = None,
) -> tuple[dict, str, bool]:
    """注册新用户，或给已有用户**轮换令牌**。

    返回 `(user, token, created)`。token 是明文，只在这一刻存在。

    顺序是刻意的：**先验邀请码再验 QQ 验证码**不好（会白烧一个邀请码），
    所以先验 QQ 码（它是一次性的、失败的代价小），通过后再消耗邀请码。
    但这样"验证码对了、邀请码错了"会浪费掉一次验证码 —— 用户得重新要一个。
    更好的做法是先只**检查**邀请码是否可用（不消耗），全通过后再真正消耗。
    """
    settings = get_settings()
    qq = normalize_qq(qq)

    existing = await get_user_by_qq(qq)

    # 已有用户再走一次 = 轮换令牌。运营者可以关掉它（比如想让令牌一旦签发就
    # 只能用到底，丢了只能人工处理）。关掉时要在**动验证码之前**就拒绝，
    # 免得白烧一个码。
    if existing is not None and not settings.allow_token_rotation:
        raise UserError(
            "这个 QQ 已经注册过了，而且本服务不允许自助轮换令牌。"
            "请联系服务提供者。",
            status_code=409,
        )

    # 1) 邀请码只对**新用户**有意义；已有用户轮换令牌不需要（他已经是用户了）
    if existing is None and settings.signups_require_invite:
        # 先检查能不能用（不消耗），避免验证码对了却在最后一步失败
        await _check_invite_usable(invite_code)

    # 2) 证明 QQ 归属
    if not await verify_code_matches(qq, code):
        raise UserError(
            "验证码不对或已过期。请在 QQ 上给机器人发一条消息重新获取。",
            status_code=401,
        )

    # 3) 验证码通过之后才真正消耗邀请码
    if existing is None and settings.signups_require_invite:
        await _consume_invite(invite_code or "")

    token = new_token()
    now = now_ms()
    if existing is not None:
        if existing.get("status") != "active":
            raise UserError("这个账号已被停用", status_code=403)
        await execute(
            "UPDATE app_user SET token_hash = ?, token_hint = ?, last_seen_at = ? WHERE id = ?",
            (hash_token(token), token_hint(token), now, existing["id"]),
        )
        logger.info("用户轮换了令牌 user=%s qq=%s", existing["id"], qq)
        return await get_user_by_id(existing["id"]) or existing, token, False

    user_id = new_id("usr")
    await execute(
        "INSERT INTO app_user (id, qq, display_name, token_hash, token_hint, status,"
        " created_at, last_seen_at) VALUES (?, ?, ?, ?, ?, 'active', ?, ?)",
        (
            user_id,
            qq,
            (display_name or "").strip() or None,
            hash_token(token),
            token_hint(token),
            now,
            now,
        ),
    )
    logger.info("新用户注册 user=%s qq=%s", user_id, qq)
    return await get_user_by_id(user_id) or {}, token, True


async def _check_invite_usable(code: str | None) -> None:
    """只检查，不消耗。"""
    normalized = (code or "").strip()
    if not normalized:
        raise UserError("需要邀请码才能注册。请向服务提供者索取。")
    row = await fetch_one("SELECT * FROM invite_code WHERE code = ?", (normalized,))
    if row is None:
        raise UserError("邀请码无效")
    if row["expires_at"] is not None and int(row["expires_at"]) < now_ms():
        raise UserError("邀请码已过期")
    if int(row["used_count"]) >= int(row["max_uses"]):
        raise UserError("邀请码已用完")


async def touch_user(user_id: str) -> None:
    """记一次活跃时间。失败无所谓，不要影响正常请求。"""
    try:
        await execute("UPDATE app_user SET last_seen_at = ? WHERE id = ?", (now_ms(), user_id))
    except Exception:  # pragma: no cover - 只是统计
        pass


async def list_users() -> list[dict]:
    rows = await fetch_all("SELECT * FROM app_user ORDER BY created_at DESC")
    return [public_user(r) for r in rows]  # type: ignore[misc]


async def count_users() -> int:
    row = await fetch_one("SELECT COUNT(*) AS n FROM app_user")
    return int(row["n"]) if row else 0
