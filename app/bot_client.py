"""调用 bot 的 HTTP 客户端。

拆分之后，后端**不再持有 OneBot 连接**，也不认识 OneBot 协议。
所有需要触达 QQ 的动作（发消息、查群名）都通过 bot 暴露的 HTTP 接口完成。

设计取舍：
  - **发送失败不抛异常**，返回 `(ok, error)`。digest 发送失败要能降级成
    "记一条失败日志"，而不是把调度循环打挂。
  - 所有请求带 `Authorization: Bearer <BOT_API_TOKEN>`（未配置则不带）。
"""

from __future__ import annotations

import logging

import httpx

from .config import Settings, get_settings

logger = logging.getLogger(__name__)

MAX_MESSAGE_CHARS = 1500


class BotUnavailable(RuntimeError):
    """bot 不可达。调用方通常应当降级而不是重试到底。"""


class BotClient:
    def __init__(self, settings: Settings | None = None):
        self.settings = settings or get_settings()
        self._client: httpx.AsyncClient | None = None

    # ------------------------------------------------------------------

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            headers = {}
            if self.settings.bot_api_token:
                headers["Authorization"] = f"Bearer {self.settings.bot_api_token}"
            self._client = httpx.AsyncClient(
                base_url=self.settings.bot_base_url.rstrip("/"),
                headers=headers,
                timeout=self.settings.bot_timeout,
            )
        return self._client

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _post(self, path: str, payload: dict) -> dict:
        try:
            resp = await self._http().post(path, json=payload)
        except httpx.HTTPError as exc:
            raise BotUnavailable(f"{type(exc).__name__}: {exc}") from exc
        if resp.status_code >= 400:
            raise BotUnavailable(f"bot 返回 HTTP {resp.status_code}: {resp.text[:200]}")
        try:
            return resp.json()
        except ValueError as exc:
            raise BotUnavailable(f"bot 返回的不是 JSON: {resp.text[:200]}") from exc

    # ------------------------------------------------------------------
    # 发送
    # ------------------------------------------------------------------

    @staticmethod
    def _truncate(message: str) -> str:
        if len(message) <= MAX_MESSAGE_CHARS:
            return message
        return message[: MAX_MESSAGE_CHARS - 8] + "\n…（已截断）"

    async def send_private(self, user_id: str, message: str) -> tuple[bool, str | None]:
        if not user_id:
            return False, "未配置接收者 QQ"
        try:
            data = await self._post(
                "/api/send/private",
                {"user_id": str(user_id), "message": self._truncate(message)},
            )
        except BotUnavailable as exc:
            return False, str(exc)
        return bool(data.get("ok")), data.get("error")

    async def send_group(self, group_id: str, message: str) -> tuple[bool, str | None]:
        try:
            data = await self._post(
                "/api/send/group",
                {"group_id": str(group_id), "message": self._truncate(message)},
            )
        except BotUnavailable as exc:
            return False, str(exc)
        return bool(data.get("ok")), data.get("error")

    # ------------------------------------------------------------------
    # 状态
    # ------------------------------------------------------------------

    async def status(self) -> dict:
        """拿 bot 的连接状态。bot 不可达时返回一个带 `reachable=False` 的结构，
        而不是抛异常 —— 健康页需要能显示"bot 挂了"这件事本身。"""
        try:
            resp = await self._http().get("/api/status")
            resp.raise_for_status()
            data = resp.json()
            data["reachable"] = True
            return data
        except Exception as exc:
            return {
                "reachable": False,
                "connected": False,
                "mode": None,
                "target": self.settings.bot_base_url,
                "last_event_at": None,
                "reconnect_count": None,
                "last_error": f"{type(exc).__name__}: {exc}",
                "groups": [],
            }


_client: BotClient | None = None


def get_bot() -> BotClient:
    global _client
    if _client is None:
        _client = BotClient()
    return _client


async def close_bot() -> None:
    global _client
    if _client is not None:
        await _client.close()
        _client = None
