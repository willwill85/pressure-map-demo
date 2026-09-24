"""本地网页服务, 对应 SleepMonitor/Program.cs 里的 ASP.NET 部分。

静态文件直接端出 ``SleepMonitor/web/``, WebSocket 挂在 ``/ws``。页面用的是原生
WebSocket, 所以 **monitor.html 一个字都不用改**, 和 Windows 版跑的是同一份。
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any, Awaitable, Callable

from aiohttp import WSMsgType, web

log = logging.getLogger(__name__)

MessageHandler = Callable[[dict[str, Any]], Awaitable[None]]
ConnectHandler = Callable[[], Awaitable[None]]


class MonitorServer:
    def __init__(self, web_root: Path, host: str = "localhost", port: int = 5000) -> None:
        self.web_root = web_root
        self.host = host
        self.port = port
        self._clients: list[web.WebSocketResponse] = []
        self._runner: web.AppRunner | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._on_message: MessageHandler | None = None
        self._on_connect: ConnectHandler | None = None

    def on_message(self, handler: MessageHandler) -> None:
        self._on_message = handler

    def on_connect(self, handler: ConnectHandler) -> None:
        self._on_connect = handler

    @property
    def client_count(self) -> int:
        return len(self._clients)

    # ---- 生命周期 ----
    async def start(self) -> None:
        if not (self.web_root / "monitor.html").is_file():
            raise FileNotFoundError(f"找不到页面文件: {self.web_root / 'monitor.html'}")

        self._loop = asyncio.get_running_loop()
        app = web.Application()
        app.router.add_get("/ws", self._handle_ws)
        app.router.add_get("/", self._handle_index)
        app.router.add_get("/{tail:.*}", self._handle_static)

        self._runner = web.AppRunner(app, access_log=None)
        await self._runner.setup()
        try:
            await web.TCPSite(self._runner, self.host, self.port).start()
        except OSError as exc:
            if exc.errno not in (48, 98, 10048):     # macOS / Linux / Windows 的"端口被占用"
                raise
            raise RuntimeError(
                f"端口 {self.port} 已被占用 —— 多半是上一次的程序还没退出。\n"
                f"  · 换个端口:  --port {self.port + 1}\n"
                f"  · 或先关掉旧进程:\n"
                f"      macOS/Linux:  pkill -f 'python -m sleepmonitor'\n"
                f"      Windows:      taskkill /F /IM python.exe\n"
                f"  · 页面右下角的「退出程序」按钮也能干净地关掉它"
            ) from None
        log.info("网页服务已启动: http://%s:%d/monitor.html", self.host, self.port)

    async def stop(self) -> None:
        for ws in list(self._clients):
            try:
                await ws.close()
            except Exception:
                pass
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None

    # ---- 路由 ----
    @staticmethod
    def _no_cache(response: web.StreamResponse) -> web.StreamResponse:
        # 与 C# 的 OnPrepareResponse 一致: 页面改完刷新即生效
        response.headers["Cache-Control"] = "no-cache, no-store"
        response.headers["Pragma"] = "no-cache"
        return response

    async def _handle_index(self, _request: web.Request) -> web.StreamResponse:
        raise web.HTTPFound("/monitor.html")

    async def _handle_static(self, request: web.Request) -> web.StreamResponse:
        tail = request.match_info.get("tail", "")
        target = (self.web_root / tail).resolve()
        try:
            target.relative_to(self.web_root.resolve())
        except ValueError:
            raise web.HTTPForbidden()
        if not target.is_file():
            raise web.HTTPNotFound()
        return self._no_cache(web.FileResponse(target))

    async def _handle_ws(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse(heartbeat=30)
        await ws.prepare(request)
        self._clients.append(ws)
        log.info("浏览器已连接（当前 %d 个连接）", len(self._clients))
        if self._on_connect is not None:
            await self._on_connect()

        try:
            async for msg in ws:
                if msg.type is not WSMsgType.TEXT:
                    continue
                try:
                    payload = json.loads(msg.data)
                except json.JSONDecodeError:
                    log.warning("收到非法 JSON: %s", msg.data[:200])
                    continue
                if self._on_message is not None:
                    try:
                        await self._on_message(payload)
                    except Exception:
                        log.exception("控制消息处理异常")
        finally:
            if ws in self._clients:
                self._clients.remove(ws)
            log.info("浏览器已断开（剩余 %d 个连接）", len(self._clients))
        return ws

    # ---- 下行 ----
    async def broadcast(self, packet: dict[str, Any]) -> None:
        if not self._clients:
            return
        text = json.dumps(packet, ensure_ascii=False)
        for ws in list(self._clients):
            if ws.closed:
                if ws in self._clients:
                    self._clients.remove(ws)
                continue
            try:
                await ws.send_str(text)
            except Exception:
                if ws in self._clients:
                    self._clients.remove(ws)
