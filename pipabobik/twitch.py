"""Twitch: OAuth (authorization code + refresh), Helix API, чат через EventSub WebSocket.

Використовується офіційний сучасний шлях Twitch:
  * читання чату — EventSub `channel.chat.message` по WebSocket;
  * надсилання — Helix `POST /chat/messages`.
"""
from __future__ import annotations

import asyncio
import json
import logging
import secrets
import time
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable

import aiohttp

log = logging.getLogger("pipabobik.twitch")

AUTH_URL = "https://id.twitch.tv/oauth2/authorize"
TOKEN_URL = "https://id.twitch.tv/oauth2/token"
VALIDATE_URL = "https://id.twitch.tv/oauth2/validate"
HELIX = "https://api.twitch.tv/helix"
EVENTSUB_WS = "wss://eventsub.wss.twitch.tv/ws?keepalive_timeout_seconds=30"
SCOPES = ["user:read:chat", "user:write:chat", "user:bot"]

MOD_BADGES = {"broadcaster", "moderator", "lead_moderator"}


class AuthError(Exception):
    pass


@dataclass
class ChatMessage:
    message_id: str
    user_id: str
    login: str
    display_name: str
    text: str
    badges: set[str] = field(default_factory=set)

    @property
    def is_broadcaster(self) -> bool:
        return "broadcaster" in self.badges

    @property
    def is_mod(self) -> bool:
        return bool(self.badges & MOD_BADGES)

    @property
    def is_vip(self) -> bool:
        return "vip" in self.badges

    @property
    def is_sub(self) -> bool:
        return bool(self.badges & {"subscriber", "founder"})

    @classmethod
    def from_event(cls, ev: dict) -> "ChatMessage":
        text = (ev.get("message") or {}).get("text", "")
        # якщо це відповідь на повідомлення, Twitch додає на початок "@нік "
        reply = ev.get("reply")
        if reply and reply.get("parent_user_login"):
            prefix = "@" + reply["parent_user_login"]
            if text.lower().startswith(prefix.lower()):
                text = text[len(prefix):]
        return cls(
            message_id=ev.get("message_id", ""),
            user_id=ev.get("chatter_user_id", ""),
            login=ev.get("chatter_user_login", ""),
            display_name=ev.get("chatter_user_name") or ev.get("chatter_user_login", ""),
            text=text.strip(),
            badges={b.get("set_id", "") for b in ev.get("badges") or []},
        )


class TwitchAuth:
    """Зберігає та оновлює токен користувача (акаунта бота)."""

    def __init__(self, client_id: str, client_secret: str, token_path: Path, redirect_uri: str):
        self.client_id = client_id
        self.client_secret = client_secret
        self.token_path = token_path
        self.redirect_uri = redirect_uri
        self.access_token: str | None = None
        self.refresh_token: str | None = None
        self.user_id: str | None = None
        self.login: str | None = None
        self._state = secrets.token_urlsafe(16)
        self._lock = asyncio.Lock()
        self.authorized = asyncio.Event()
        self._load()

    # ── збереження ──
    def _load(self) -> None:
        if self.token_path.exists():
            try:
                d = json.loads(self.token_path.read_text(encoding="utf-8"))
                self.access_token = d.get("access_token")
                self.refresh_token = d.get("refresh_token")
            except (OSError, ValueError):
                log.warning("Не вдалося прочитати %s", self.token_path)

    def _save(self) -> None:
        self.token_path.parent.mkdir(parents=True, exist_ok=True)
        self.token_path.write_text(
            json.dumps({"access_token": self.access_token, "refresh_token": self.refresh_token}),
            encoding="utf-8",
        )

    # ── OAuth ──
    def authorize_url(self) -> str:
        q = {
            "response_type": "code",
            "client_id": self.client_id,
            "redirect_uri": self.redirect_uri,
            "scope": " ".join(SCOPES),
            "state": self._state,
            "force_verify": "true",  # дозволяє обрати інший акаунт (бота)
        }
        return AUTH_URL + "?" + urllib.parse.urlencode(q)

    async def exchange_code(self, session: aiohttp.ClientSession, code: str, state: str) -> None:
        if state != self._state:
            raise AuthError("Невірний state — спробуйте авторизуватися ще раз")
        data = {
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "code": code,
            "grant_type": "authorization_code",
            "redirect_uri": self.redirect_uri,
        }
        async with session.post(TOKEN_URL, data=data) as r:
            body = await r.json(content_type=None)
            if r.status != 200:
                raise AuthError(f"Twitch відмовив: {body.get('message', body)}")
        self.access_token = body["access_token"]
        self.refresh_token = body.get("refresh_token")
        self._save()
        await self.validate(session)

    async def refresh(self, session: aiohttp.ClientSession) -> None:
        async with self._lock:
            if not self.refresh_token:
                raise AuthError("Немає refresh-токена — потрібна повторна авторизація")
            data = {
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "grant_type": "refresh_token",
                "refresh_token": self.refresh_token,
            }
            async with session.post(TOKEN_URL, data=data) as r:
                body = await r.json(content_type=None)
                if r.status != 200:
                    self.authorized.clear()
                    raise AuthError(f"Не вдалося оновити токен: {body.get('message', body)}")
            self.access_token = body["access_token"]
            self.refresh_token = body.get("refresh_token", self.refresh_token)
            self._save()
            log.info("Токен Twitch оновлено")

    async def validate(self, session: aiohttp.ClientSession) -> bool:
        """Перевіряє токен (Twitch вимагає робити це щогодини). Оновлює за потреби."""
        if not self.access_token:
            return False
        for attempt in range(2):
            async with session.get(VALIDATE_URL, headers={"Authorization": f"OAuth {self.access_token}"}) as r:
                if r.status == 200:
                    d = await r.json()
                    missing = set(SCOPES[:2]) - set(d.get("scopes") or [])
                    if missing:
                        log.error("У токена бракує прав %s — авторизуйтесь ще раз", missing)
                        return False
                    self.user_id, self.login = d["user_id"], d["login"]
                    self.authorized.set()
                    return True
            if attempt == 0 and self.refresh_token:
                try:
                    await self.refresh(session)
                    continue
                except AuthError as e:
                    log.error("%s", e)
            break
        self.authorized.clear()
        return False


class Helix:
    def __init__(self, auth: TwitchAuth, session: aiohttp.ClientSession):
        self.auth = auth
        self.session = session

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.auth.access_token}",
            "Client-Id": self.auth.client_id,
            "Content-Type": "application/json",
        }

    async def request(self, method: str, path: str, *, params=None, body=None) -> tuple[int, dict]:
        for attempt in range(2):
            async with self.session.request(
                method, HELIX + path, params=params, json=body, headers=self._headers()
            ) as r:
                if r.status == 401 and attempt == 0:
                    await self.auth.refresh(self.session)
                    continue
                if r.status == 429:
                    reset = float(r.headers.get("Ratelimit-Reset", time.time() + 1))
                    await asyncio.sleep(max(0.5, reset - time.time()))
                    continue
                try:
                    data = await r.json(content_type=None)
                except (ValueError, aiohttp.ContentTypeError):
                    data = {}
                return r.status, data or {}
        return 0, {}

    async def get_user_id(self, login: str) -> str | None:
        status, d = await self.request("GET", "/users", params={"login": login})
        items = d.get("data") or []
        return items[0]["id"] if status == 200 and items else None

    async def send_chat(self, broadcaster_id: str, text: str, reply_to: str | None = None) -> bool:
        body = {"broadcaster_id": broadcaster_id, "sender_id": self.auth.user_id, "message": text[:500]}
        if reply_to:
            body["reply_parent_message_id"] = reply_to
        status, d = await self.request("POST", "/chat/messages", body=body)
        ok = status == 200 and (d.get("data") or [{}])[0].get("is_sent", False)
        if not ok:
            log.warning("Повідомлення не надіслано (%s): %s", status, d)
        return ok

    async def subscribe_chat(self, session_id: str, broadcaster_id: str) -> None:
        body = {
            "type": "channel.chat.message",
            "version": "1",
            "condition": {"broadcaster_user_id": broadcaster_id, "user_id": self.auth.user_id},
            "transport": {"method": "websocket", "session_id": session_id},
        }
        status, d = await self.request("POST", "/eventsub/subscriptions", body=body)
        if status not in (202, 409):
            raise AuthError(f"Не вдалося підписатися на чат ({status}): {d.get('message', d)}")


MessageHandler = Callable[[ChatMessage], Awaitable[None]]


class TwitchChat:
    """Підключення до чату через EventSub WebSocket з автоматичним перепідключенням."""

    def __init__(self, auth: TwitchAuth, session: aiohttp.ClientSession, channel: str):
        self.auth = auth
        self.session = session
        self.helix = Helix(auth, session)
        self.channel = channel
        self.broadcaster_id: str | None = None
        self.handler: MessageHandler | None = None
        self.connected = asyncio.Event()

    async def say(self, text: str, reply_to: str | None = None) -> None:
        if not self.broadcaster_id:
            log.info("[чат офлайн] %s", text)
            return
        try:
            await self.helix.send_chat(self.broadcaster_id, text, reply_to)
        except (aiohttp.ClientError, AuthError, asyncio.TimeoutError) as e:
            log.warning("Помилка надсилання в чат: %s", e)

    async def run(self) -> None:
        backoff = 1
        while True:
            await self.auth.authorized.wait()
            try:
                if not self.broadcaster_id:
                    self.broadcaster_id = await self.helix.get_user_id(self.channel)
                    if not self.broadcaster_id:
                        log.error("Канал «%s» не знайдено на Twitch — перевірте config.toml", self.channel)
                        await asyncio.sleep(30)
                        continue
                await self._session_loop(EVENTSUB_WS, subscribe=True)
                backoff = 1
            except AuthError as e:
                log.error("%s", e)
                self.auth.authorized.clear()
            except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as e:
                log.warning("Зв'язок з Twitch втрачено (%s), перепідключаюсь через %sс", e, backoff)
            self.connected.clear()
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)

    async def _session_loop(self, url: str, subscribe: bool) -> None:
        async with self.session.ws_connect(url, heartbeat=None) as ws:
            keepalive = 40.0
            while True:
                msg = await asyncio.wait_for(ws.receive(), timeout=keepalive + 10)
                if msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.ERROR):
                    raise aiohttp.ClientError(f"WebSocket закрито ({ws.close_code})")
                if msg.type != aiohttp.WSMsgType.TEXT:
                    continue
                data = json.loads(msg.data)
                mtype = data.get("metadata", {}).get("message_type")
                payload = data.get("payload", {})
                if mtype == "session_welcome":
                    sess = payload["session"]
                    keepalive = float(sess.get("keepalive_timeout_seconds") or 30)
                    if subscribe:
                        await self.helix.subscribe_chat(sess["id"], self.broadcaster_id)
                    self.connected.set()
                    log.info("Чат підключено: #%s (бот: %s)", self.channel, self.auth.login)
                elif mtype == "notification":
                    ev = payload.get("event", {})
                    if payload.get("subscription", {}).get("type") == "channel.chat.message":
                        cm = ChatMessage.from_event(ev)
                        if cm.user_id != self.auth.user_id and self.handler:
                            asyncio.create_task(self._safe_handle(cm))
                elif mtype == "session_reconnect":
                    new_url = payload["session"]["reconnect_url"]
                    log.info("Twitch просить перепідключитися")
                    # підписки переносяться автоматично — повторно не підписуємось
                    await self._session_loop(new_url, subscribe=False)
                    return
                elif mtype == "revocation":
                    raise AuthError(f"Twitch відкликав підписку: {payload.get('subscription', {}).get('status')}")

    async def _safe_handle(self, cm: ChatMessage) -> None:
        try:
            await self.handler(cm)
        except Exception:  # noqa: BLE001
            log.exception("Помилка обробки повідомлення: %s", cm.text)
