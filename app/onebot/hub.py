"""OneBot 11 连接管理。

支持两种模式（NapCat 那边要跟着配）：
  - client：本服务主动连 NapCat 的 WebSocket 服务地址
  - server：本服务监听一个 WS 端口，NapCat 用「反向 WebSocket」连过来

两种模式共用同一个连接对象，所以下游（ingest / digest）不需要关心用的是哪种。
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Awaitable, Callable

from ..config import Settings, get_settings
from ..utils import new_id, now_ms

logger = logging.getLogger(__name__)

try:  # websockets >= 13 的新 asyncio 实现
    from websockets.asyncio.client import connect as _ws_connect  # type: ignore
except Exception:  # pragma: no cover - 兼容 websockets 12 的旧路径
    from websockets.client import connect as _ws_connect  # type: ignore

MAX_FRAME = 32 * 1024 * 1024  # 合并转发的消息体可能很大


class OneBotNotConnected(RuntimeError):
    pass


class _Conn:
    """把 websockets 与 Starlette WebSocket 归一化成 send/recv。"""

    def __init__(self, ws: Any, kind: str):
        self.ws = ws
        self.kind = kind  # 'client' | 'server'

    async def send_text(self, text: str) -> None:
        if self.kind == "server":
            await self.ws.send_text(text)
        else:
            await self.ws.send(text)

    async def recv_text(self) -> str:
        if self.kind == "server":
            return await self.ws.receive_text()
        return await self.ws.recv()

    async def close(self) -> None:
        try:
            if self.kind == "server":
                await self.ws.close()
            else:
                await self.ws.close()
        except Exception:
            pass


class OneBotHub:
    def __init__(self, settings: Settings | None = None):
        self.settings = settings or get_settings()
        self._conn: _Conn | None = None
        self._pending: dict[str, asyncio.Future] = {}
        self._on_event: Callable[[dict], Awaitable[None]] | None = None
        self._recv_task: asyncio.Task | None = None
        self._client_task: asyncio.Task | None = None
        self._stopping = False
        self.connected = False
        self.last_event_at: int | None = None
        self.reconnect_count = 0
        self.last_error: str | None = None

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    def set_event_handler(self, handler: Callable[[dict], Awaitable[None]]) -> None:
        self._on_event = handler

    async def start(self) -> None:
        if self.settings.onebot_mode == "client":
            self._stopping = False
            self._client_task = asyncio.create_task(self._client_loop())

    async def stop(self) -> None:
        self._stopping = True
        if self._client_task:
            self._client_task.cancel()
        if self._recv_task:
            self._recv_task.cancel()
        if self._conn:
            await self._conn.close()

    # ------------------------------------------------------------------
    # client 模式
    # ------------------------------------------------------------------

    async def _client_loop(self) -> None:
        backoff = 1.0
        while not self._stopping:
            try:
                await self._connect_once()
                backoff = 1.0
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.last_error = f"{type(exc).__name__}: {exc}"
                logger.warning(
                    "OneBot 连接失败: %s，%.0fs 后重试", self.last_error, backoff
                )
                self.connected = False
                self.reconnect_count += 1
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60.0)

    async def _connect_once(self) -> None:
        url = self.settings.onebot_ws_url
        headers = {}
        if self.settings.onebot_access_token:
            headers["Authorization"] = f"Bearer {self.settings.onebot_access_token}"

        kwargs: dict[str, Any] = {
            "max_size": MAX_FRAME,
            "ping_interval": 20,
            "ping_timeout": 20,
        }
        if headers:
            kwargs["additional_headers"] = headers
        try:
            ws = await _ws_connect(url, **kwargs)
        except TypeError:
            # 旧版 websockets 用的是 extra_headers
            if headers:
                kwargs.pop("additional_headers", None)
                kwargs["extra_headers"] = headers
            ws = await _ws_connect(url, **kwargs)

        logger.info("OneBot 已连接 (client): %s", url)
        self._conn = _Conn(ws, "client")
        self.connected = True
        self.last_error = None
        try:
            await self._run_socket(self._conn)
        finally:
            self.connected = False
            self._conn = None
            await ws.close()

    # ------------------------------------------------------------------
    # server 模式（反向 WS）
    # ------------------------------------------------------------------

    async def attach_server_ws(self, websocket: Any) -> None:
        """由 FastAPI 的 websocket 路由调用。"""
        token = self.settings.onebot_access_token
        if token:
            header = websocket.headers.get("authorization", "")
            if header != f"Bearer {token}":
                await websocket.close(code=1008)
                return
        await websocket.accept()
        logger.info("OneBot 已连接 (server 反向 WS)")
        conn = _Conn(websocket, "server")
        self._conn = conn
        self.connected = True
        self.last_error = None
        try:
            await self._run_socket(conn)
        finally:
            self.connected = False
            self._conn = None

    # ------------------------------------------------------------------
    # 收发
    # ------------------------------------------------------------------

    async def _run_socket(self, conn: _Conn) -> None:
        while True:
            text = await conn.recv_text()
            if text is None:
                break
            try:
                data = json.loads(text)
            except json.JSONDecodeError:
                logger.warning("OneBot 收到非法 JSON: %s", text[:200])
                continue
            await self._dispatch(data)

    async def _dispatch(self, data: dict) -> None:
        # 先处理 API 响应（带 echo）
        echo = data.get("echo")
        if echo is not None:
            fut = self._pending.get(str(echo))
            if fut and not fut.done():
                fut.set_result(data)
            return

        self.last_event_at = now_ms()
        post_type = data.get("post_type")
        if post_type in ("message", "message_sent"):
            if self._on_event is None:
                return
            # 不阻塞接收循环，否则合并转发展开会卡住整个连接
            asyncio.create_task(self._safe_handle(data))
        elif post_type == "meta_event":
            # 心跳，仅用于刷新 last_event_at
            pass

    async def _safe_handle(self, event: dict) -> None:
        assert self._on_event is not None
        try:
            await self._on_event(event)
        except Exception as exc:  # 单条消息失败绝不能让接收循环挂掉
            logger.exception("消息处理失败: %s", exc)

    async def call_api(self, action: str, params: dict | None = None, timeout: float = 15.0) -> dict:
        conn = self._conn
        if conn is None:
            raise OneBotNotConnected("OneBot 未连接")
        echo = new_id()
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        self._pending[echo] = fut
        try:
            await conn.send_text(json.dumps({"action": action, "params": params or {}, "echo": echo}))
            return await asyncio.wait_for(fut, timeout=timeout)
        finally:
            self._pending.pop(echo, None)

    # ------------------------------------------------------------------
    # 常用动作
    # ------------------------------------------------------------------

    async def send_private_msg(self, user_id: str, message: str) -> dict:
        return await self.call_api(
            "send_private_msg",
            {"user_id": int(user_id) if str(user_id).isdigit() else user_id, "message": message},
            timeout=20,
        )

    async def get_forward_msg(self, forward_id: str) -> list[dict]:
        resp = await self.call_api("get_forward_msg", {"id": forward_id})
        data = resp.get("data") or {}
        return data.get("messages") or []

    async def get_group_info(self, group_id: str) -> dict:
        resp = await self.call_api("get_group_info", {"group_id": group_id, "no_cache": False})
        return resp.get("data") or {}

    # ------------------------------------------------------------------
    # 状态
    # ------------------------------------------------------------------

    def status(self) -> dict:
        target = (
            self.settings.onebot_ws_url
            if self.settings.onebot_mode == "client"
            else f"{self.settings.onebot_listen_host}:{self.settings.onebot_listen_port}{self.settings.onebot_listen_path}"
        )
        return {
            "mode": self.settings.onebot_mode,
            "connected": self.connected,
            "target": target,
            "last_event_at": self.last_event_at,
            "reconnect_count": self.reconnect_count,
            "last_error": self.last_error,
        }


_hub: OneBotHub | None = None


def get_hub() -> OneBotHub:
    global _hub
    if _hub is None:
        _hub = OneBotHub()
    return _hub


def reset_hub() -> None:
    global _hub
    _hub = None
