"""Збирає все докупи: Twitch-чат, черга, плеєр, веб-сервер, стеження за OBS."""
from __future__ import annotations

import asyncio
import logging
import logging.handlers
import sys
import time
import webbrowser

import aiohttp

from .commands import Commands
from .config import DATA_DIR, Config
from .jukebox import Jukebox
from .music import MusicSource
from .player import Player
from .server import Server
from .twitch import TwitchAuth, TwitchChat

log = logging.getLogger("pipabobik")

OBS_PROCESS_NAMES = {"obs64.exe", "obs32.exe", "obs.exe", "obs"}


def setup_logging(verbose: bool = False) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s", "%H:%M:%S")
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    fh = logging.handlers.RotatingFileHandler(DATA_DIR / "pipabobik.log", maxBytes=1_000_000, backupCount=2, encoding="utf-8")
    fh.setFormatter(fmt)
    root.addHandler(fh)
    if sys.stdout is not None:  # під pythonw консолі немає — пишемо лише у файл
        sh = logging.StreamHandler(sys.stdout)
        sh.setFormatter(fmt)
        root.addHandler(sh)
    for noisy in ("aiohttp.access", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


class Bot:
    def __init__(self, cfg: Config, obs_mode: bool = False, live: bool = False, paused: bool = False):
        self.cfg = cfg
        self.obs_mode = obs_mode
        self.start_live = live
        self.start_paused = paused
        self.obs_live: bool | None = None  # None — бот запущено не з OBS
        self.google_login: bool | None = None
        self.session: aiohttp.ClientSession | None = None
        self.shutdown = asyncio.Event()

        tw = cfg["twitch"]
        self.auth = TwitchAuth(
            tw["client_id"], tw["client_secret"], DATA_DIR / "twitch_token.json",
            f"http://localhost:{cfg.port}/auth/callback",
        )
        g = cfg["google"]
        self.music = MusicSource(g["cookies_from_browser"], g["cookies_file"])
        p = cfg["player"]
        self.player = Player(p["mpv_path"], p["volume"], p["normalize"], p["audio_device"], p["show_window"])
        self.jukebox = Jukebox(cfg["rules"], g, self.music, self.player, DATA_DIR)
        self.jukebox.announce = bool(cfg["chat"].get("announce_tracks"))
        self.server = Server(self, cfg.port, DATA_DIR)
        self.chat: TwitchChat | None = None
        self._greeted = 0.0

    def health(self) -> dict:
        return {
            "twitch": self.auth.login if self.auth.authorized.is_set() else None,
            "channel": self.cfg.channel,
            "chat": bool(self.chat and self.chat.connected.is_set()),
            "mpv": self.player.available,
            "google": self.google_login,
            "obs_live": self.obs_live,
        }

    def apply_settings(self, updates: dict) -> None:
        """Застосовує зміни з панелі, які не підхоплюються самі."""
        if "chat" in updates:
            self.jukebox.announce = bool(self.cfg["chat"].get("announce_tracks"))
        if "player" in updates and "normalize" in updates["player"]:
            self.player.normalize = bool(updates["player"]["normalize"])  # з наступного треку
        if "google" in updates:
            self.jukebox.reset_fallback()
        log.info("Налаштування оновлено з панелі: %s", ", ".join(f"{s}.{k}" for s, kv in updates.items() for k in kv))
        self.jukebox.changed()

    def _detect_google_login(self) -> bool | None:
        g = self.cfg["google"]
        if not (g.get("cookies_from_browser") or g.get("cookies_file")):
            return None
        try:
            import yt_dlp

            with yt_dlp.YoutubeDL(self.music.ydl_opts()) as ydl:
                return any(c.name in ("SAPISID", "__Secure-3PAPISID") for c in ydl.cookiejar if "youtube.com" in c.domain)
        except Exception:  # noqa: BLE001
            return False

    async def say(self, text: str, reply_to: str | None = None) -> None:
        if self.chat:
            await self.chat.say(text, reply_to)

    async def run(self) -> int:
        try:
            await self.server.start()
        except OSError:
            log.info("pipabobik вже запущено (порт %s зайнятий) — виходжу", self.cfg.port)
            return 0
        self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30))
        self.chat = TwitchChat(self.auth, self.session, self.cfg.channel)
        self.commands = Commands(self.cfg, self.jukebox, self.music, self.say)
        self.chat.handler = self.commands.handle
        self.jukebox.say = lambda text: self.say(text)
        self.jukebox.listeners.append(self.server.push)

        base = f"http://localhost:{self.cfg.port}"
        log.info("🎀 pipabobik запущено · віджет: %s/widget · панель: %s/panel?key=%s", base, base, self.server.key)
        if not self.player.available:
            log.error("mpv не знайдено! Встановіть: winget install -e --id shinchiro.mpv")

        if not self.cfg.channel or not self.auth.client_id:
            log.error("Заповніть [twitch] channel, client_id, client_secret у config.toml (або запустіть --setup)")
        elif not await self.auth.validate(self.session):
            log.warning("Потрібно увійти в Twitch: відкриваю %s/auth", base)
            webbrowser.open(f"{base}/auth")

        tasks = [
            asyncio.create_task(self.chat.run(), name="chat"),
            asyncio.create_task(self.jukebox.run(), name="jukebox"),
            asyncio.create_task(self._validate_loop(), name="validate"),
        ]
        if self.obs_mode:
            tasks.append(asyncio.create_task(self._watch_obs(), name="obs"))
        if self.start_live or not (self.obs_mode or self.start_paused):
            self.jukebox.activate()
        asyncio.create_task(self._check_google())
        try:
            await self.shutdown.wait()
        finally:
            log.info("Завершення роботи…")
            self.jukebox.save()
            await self.player.stop()
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await self.server.stop()
            await self.session.close()
        return 0

    async def _check_google(self) -> None:
        self.google_login = await asyncio.to_thread(self._detect_google_login)
        self.jukebox.changed()

    async def _validate_loop(self) -> None:
        """Twitch вимагає перевіряти токен щогодини."""
        while True:
            await asyncio.sleep(3600)
            try:
                await self.auth.validate(self.session)
            except aiohttp.ClientError as e:
                log.warning("validate: %s", e)

    async def _greet(self) -> None:
        await self.chat.connected.wait()
        c = self.commands.first
        await self.say(f"🎀 pipabobik на місці! Замовляй музику: {c('request')} назва · що грає: {c('now')}")

    async def _watch_obs(self) -> None:
        """Стежить за файлом стану, який пише Lua-скрипт OBS, і за процесом OBS."""
        state_file = DATA_DIR / "obs_state"
        last = None
        missing = 0
        tick = 0
        while True:
            await asyncio.sleep(1)
            tick += 1
            try:
                state = state_file.read_text(encoding="utf-8").strip().split()[0] if state_file.exists() else ""
            except (OSError, IndexError):
                state = ""
            if state != last:
                self.obs_live = state == "live"
                self.jukebox.changed()
                if state == "live":
                    log.info("OBS: трансляцію розпочато")
                    self.jukebox.activate()
                    if self.cfg["chat"].get("greet_on_stream_start") and time.time() - self._greeted > 1800:
                        self._greeted = time.time()
                        asyncio.create_task(self._greet())
                elif state == "idle" and last == "live":
                    log.info("OBS: трансляцію завершено — музику на паузу, черга збережена")
                    await self.jukebox.deactivate()
                elif state == "exit":
                    log.info("OBS закрито — бот теж закривається")
                    self.shutdown.set()
                    return
                last = state
            if tick % 15 == 0:
                if await asyncio.to_thread(self._obs_running):
                    missing = 0
                else:
                    missing += 1
                    if missing >= 3:
                        log.info("Процес OBS не знайдено — бот закривається")
                        self.shutdown.set()
                        return

    @staticmethod
    def _obs_running() -> bool:
        try:
            import psutil
        except ImportError:
            return True
        for p in psutil.process_iter(["name"]):
            if (p.info.get("name") or "").lower() in OBS_PROCESS_NAMES:
                return True
        return False
