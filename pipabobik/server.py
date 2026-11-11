"""Локальний веб-сервер: віджет для OBS, панель керування, авторизація Twitch."""
from __future__ import annotations

import asyncio
import json
import logging
import secrets
from pathlib import Path

from aiohttp import web

log = logging.getLogger("pipabobik.server")

WEB_DIR = Path(__file__).resolve().parent.parent / "web"


class Server:
    def __init__(self, bot, port: int, data_dir: Path):
        self.bot = bot
        self.port = port
        self.key = self._load_key(data_dir)
        self._clients: set[asyncio.Queue] = set()
        self.app = web.Application()
        r = self.app.router
        r.add_get("/", self.index)
        r.add_get("/widget", self.page("widget.html"))
        r.add_get("/panel", self.page("panel.html"))
        r.add_get("/api/state", self.api_state)
        r.add_get("/api/events", self.api_events)
        r.add_post("/api/control", self.api_control)
        r.add_get("/auth", self.auth_start)
        r.add_get("/auth/callback", self.auth_callback)
        r.add_static("/static", WEB_DIR / "static")
        self.runner: web.AppRunner | None = None

    @staticmethod
    def _load_key(data_dir: Path) -> str:
        p = data_dir / "panel.key"
        if p.exists():
            return p.read_text().strip()
        data_dir.mkdir(parents=True, exist_ok=True)
        k = secrets.token_urlsafe(12)
        p.write_text(k)
        return k

    async def start(self) -> None:
        self.runner = web.AppRunner(self.app, access_log=None)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", self.port)
        await site.start()  # OSError, якщо порт зайнятий (бот вже запущено)

    async def stop(self) -> None:
        if self.runner:
            await self.runner.cleanup()

    # ── стан ──
    def push(self) -> None:
        data = json.dumps(self.bot.jukebox.state(), ensure_ascii=False)
        for q in list(self._clients):
            if q.qsize() < 20:
                q.put_nowait(data)

    async def api_state(self, request: web.Request) -> web.Response:
        return web.json_response(self.bot.jukebox.state(), headers={"Access-Control-Allow-Origin": "*"})

    async def api_events(self, request: web.Request) -> web.StreamResponse:
        resp = web.StreamResponse(
            headers={
                "Content-Type": "text/event-stream",
                "Cache-Control": "no-cache",
                "Access-Control-Allow-Origin": "*",
            }
        )
        await resp.prepare(request)
        q: asyncio.Queue = asyncio.Queue()
        self._clients.add(q)
        try:
            await resp.write(f"data: {json.dumps(self.bot.jukebox.state(), ensure_ascii=False)}\n\n".encode())
            while True:
                try:
                    data = await asyncio.wait_for(q.get(), timeout=15)
                    await resp.write(f"data: {data}\n\n".encode())
                except asyncio.TimeoutError:
                    await resp.write(b": ping\n\n")
        except (ConnectionResetError, asyncio.CancelledError):
            pass
        finally:
            self._clients.discard(q)
        return resp

    # ── сторінки ──
    def page(self, name: str):
        async def handler(request: web.Request) -> web.Response:
            return web.FileResponse(WEB_DIR / name, headers={"Cache-Control": "no-cache"})

        return handler

    async def index(self, request: web.Request) -> web.Response:
        b = self.bot
        authed = b.auth.authorized.is_set()
        base = f"http://localhost:{self.port}"
        rows = [
            ("Twitch", f"✅ увійшов як <b>{b.auth.login}</b>" if authed else '❌ <a href="/auth">увійти в Twitch</a>'),
            ("Канал", f"#{b.cfg.channel}" if b.cfg.channel else "❌ не вказано в config.toml"),
            ("Чат", "✅ підключено" if b.chat.connected.is_set() else "⏳ підключення…"),
            ("mpv", "✅ знайдено" if b.player.available else "❌ не знайдено — встановіть mpv"),
            ("Віджет", f'<code>{base}/widget</code>'),
            ("Панель (док OBS)", f'<code>{base}/panel?key={self.key}</code>'),
        ]
        html = (
            "<!doctype html><meta charset=utf-8><title>pipabobik</title>"
            "<style>body{font:15px system-ui;background:#1a0f17;color:#fbe7f2;padding:32px}"
            "h1{color:#ff5fa8}td{padding:8px 14px;border-bottom:1px solid #3a2232}"
            "a{color:#ff8cc6}code{background:#2c1726;padding:3px 6px;border-radius:6px}</style>"
            "<h1>🎀 pipabobik</h1><table>"
            + "".join(f"<tr><td>{k}</td><td>{v}</td></tr>" for k, v in rows)
            + "</table>"
        )
        return web.Response(text=html, content_type="text/html")

    # ── керування з панелі ──
    async def api_control(self, request: web.Request) -> web.Response:
        if request.headers.get("X-Pipabobik-Key") != self.key:
            return web.json_response({"error": "bad key"}, status=403)
        d = await request.json()
        jb = self.bot.jukebox
        action = d.get("action")
        if action == "skip":
            await jb.skip()
        elif action == "pause":
            await jb.set_pause(not jb.player.paused)
        elif action == "volume":
            await jb.set_volume(int(d.get("value", jb.player.volume)))
        elif action == "remove":
            jb.remove_at(int(d.get("index", -1)))
        elif action == "up":
            jb.move_up(int(d.get("index", -1)))
        elif action == "ban":
            await jb.ban_current()
        elif action == "on":
            jb.activate()
        elif action == "off":
            await jb.deactivate()
        else:
            return web.json_response({"error": "unknown action"}, status=400)
        return web.json_response(jb.state())

    # ── авторизація Twitch ──
    async def auth_start(self, request: web.Request) -> web.Response:
        if not self.bot.auth.client_id:
            return web.Response(text="Вкажіть client_id у config.toml", status=400)
        raise web.HTTPFound(self.bot.auth.authorize_url())

    async def auth_callback(self, request: web.Request) -> web.Response:
        code, state = request.query.get("code"), request.query.get("state", "")
        if not code:
            err = request.query.get("error_description") or "авторизацію скасовано"
            return web.Response(text=f"❌ {err}", content_type="text/plain", charset="utf-8")
        try:
            await self.bot.auth.exchange_code(self.bot.session, code, state)
        except Exception as e:  # noqa: BLE001
            log.exception("auth")
            return web.Response(text=f"❌ {e}", content_type="text/plain", charset="utf-8")
        html = (
            "<!doctype html><meta charset=utf-8><body style='font:18px system-ui;background:#1a0f17;"
            "color:#fbe7f2;display:grid;place-items:center;height:90vh'>"
            f"<div>✅ Готово! pipabobik увійшов як <b>{self.bot.auth.login}</b>. Вкладку можна закрити.</div>"
        )
        return web.Response(text=html, content_type="text/html")
