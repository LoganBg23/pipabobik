"""Команди чату."""
from __future__ import annotations

import logging
from typing import Awaitable, Callable

from .jukebox import Jukebox
from .music import MusicSource, Track, extract_video_id, fmt_time
from .twitch import ChatMessage

log = logging.getLogger("pipabobik.commands")

Say = Callable[[str, str | None], Awaitable[None]]


def minutes(sec: int) -> str:
    if sec < 60:
        return "менш ніж хвилину"
    return f"~{round(sec / 60)} хв"


class Commands:
    def __init__(self, cfg, jukebox: Jukebox, music: MusicSource, say: Say):
        self.cfg = cfg
        self.jb = jukebox
        self.music = music
        self.say = say
        self.table: dict[str, Callable[[ChatMessage, str], Awaitable[None]]] = {}
        handlers = {
            "request": self.cmd_request,
            "now": self.cmd_now,
            "skip": self.cmd_skip,
            "queue": self.cmd_queue,
            "wrong": self.cmd_wrong,
            "volume": self.cmd_volume,
            "pause": self.cmd_pause,
            "play": self.cmd_play,
            "ban": self.cmd_ban,
            "help": self.cmd_help,
        }
        for key, fn in handlers.items():
            for alias in cfg["commands"].get(key, []):
                self.table[alias] = fn

    def first(self, key: str) -> str:
        names = self.cfg["commands"].get(key) or [f"!{key}"]
        return names[0]

    def privileged(self, m: ChatMessage) -> bool:
        return m.is_mod or m.login.lower() in self.cfg["twitch"]["admins"]

    def helper(self, m: ChatMessage) -> bool:
        """Помічник зі списку в застосунку: лише скіп і гучність."""
        return m.login.lower() in (self.cfg["twitch"].get("helpers") or [])

    def can_control(self, m: ChatMessage) -> bool:
        return self.privileged(m) or self.helper(m)

    def can_request(self, m: ChatMessage) -> bool:
        who = self.cfg["rules"].get("who_can_request", "all")
        if who == "all" or self.privileged(m):
            return True
        if who == "vip":
            return m.is_vip
        if who == "subs":
            return m.is_sub or m.is_vip
        return False  # mods

    async def reply(self, m: ChatMessage, text: str) -> None:
        await self.say(text, m.message_id)

    async def handle(self, m: ChatMessage) -> None:
        if not m.text.startswith("!"):
            return
        parts = m.text.split(maxsplit=1)
        name = parts[0].lower()
        arg = parts[1].strip() if len(parts) > 1 else ""
        fn = self.table.get(name)
        if fn:
            log.info("%s: %s", m.login, m.text)
            await fn(m, arg)

    # ── для всіх ──
    async def cmd_request(self, m: ChatMessage, arg: str) -> None:
        if not self.can_request(m):
            return await self.reply(m, "замовлення зараз доступні не всім глядачам 🙈")
        if not arg:
            return await self.reply(m, f"напиши назву: {self.first('request')} виконавець — пісня 🎵")
        priv = self.privileged(m)
        reason = self.jb.check_user(m.user_id, priv)
        if reason:
            return await self.reply(m, reason)

        vid = extract_video_id(arg)
        if vid:
            t = await self.music.by_id(vid)
            candidates = [t] if t else []
        elif "list=" in arg and "http" in arg:
            return await self.reply(m, "плейлисти замовляти не можна — лише окремі треки 🙂")
        else:
            candidates = await self.music.search(arg, only_songs=bool(self.cfg["rules"].get("only_songs", True)))
        if not candidates:
            return await self.reply(m, f"нічого не знайшов за «{arg[:60]}» 🤷 Спробуй «виконавець — назва»")

        chosen: Track | None = None
        first_reason = None
        for t in candidates[:5]:
            r = self.jb.check(t, m.user_id, priv)
            if r is None:
                ru = await self.jb.filter_reason(t)
                if ru:
                    # не підсовуємо інший трек замість забороненого — просто відмовляємо
                    self.jb.ban_track(t, ru)
                    return await self.reply(m, f"{t.label} — {ru}. Російське тут не граємо 🇺🇦")
                chosen = t
                break
            first_reason = first_reason or r
        if not chosen:
            return await self.reply(m, first_reason or "цей трек не можна додати")

        # повторна перевірка: поки шукали, ліміти могли змінитися
        reason = self.jb.check_user(m.user_id, priv)
        if reason:
            return await self.reply(m, reason)
        pos = self.jb.add(chosen, m.user_id, m.display_name)
        dur = f" ({fmt_time(chosen.duration)})" if chosen.duration else ""
        if self.jb.active:
            wait = self.jb.eta(pos)
            when = "грає наступним" if wait < 15 else f"заграє через {minutes(wait)}"
            await self.reply(m, f"🎶 додано {chosen.label}{dur} — №{pos} у черзі, {when}")
        else:
            await self.reply(m, f"🎶 додано {chosen.label}{dur} — №{pos} у черзі, заграє, коли почнеться музика")

    async def cmd_now(self, m: ChatMessage, arg: str) -> None:
        t = self.jb.now
        if not t:
            if not self.jb.active:
                return await self.reply(m, "музика зараз вимкнена 🔇")
            return await self.reply(m, f"зараз тиша 🤫 Замов щось: {self.first('request')} назва")
        pos = fmt_time(self.jb.player.position)
        dur = f" / {fmt_time(t.duration)}" if t.duration else ""
        who = f" · замовив(ла) @{t.requested_by}" if t.source == "request" else " · з плейлиста стрімера"
        await self.reply(m, f"🎧 зараз грає {t.label} [{pos}{dur}]{who} → {t.url}")

    async def cmd_queue(self, m: ChatMessage, arg: str) -> None:
        q = self.jb.queue
        if not q:
            return await self.reply(m, f"черга порожня — замов: {self.first('request')} назва 🎵")
        items = [f"{i}) {t.label} (@{t.requested_by})" for i, t in enumerate(q[:4], 1)]
        more = f" …і ще {len(q) - 4}" if len(q) > 4 else ""
        await self.reply(m, "далі: " + " · ".join(items) + more)

    async def cmd_wrong(self, m: ChatMessage, arg: str) -> None:
        t = self.jb.remove_last_by(m.user_id)
        if t:
            await self.reply(m, f"прибрав з черги {t.label} 👌")
        else:
            await self.reply(m, "у тебе немає треків у черзі")

    async def cmd_help(self, m: ChatMessage, arg: str) -> None:
        c = self.first
        text = (
            f"🎵 pipabobik: {c('request')} назва — замовити · {c('now')} — що грає · "
            f"{c('queue')} — черга · {c('wrong')} — прибрати свій трек"
        )
        if self.privileged(m):
            text += f" · модерам: {c('skip')}, {c('pause')}, {c('play')}, {c('volume')} 0-100, {c('ban')}"
        elif self.helper(m):
            text += f" · тобі можна: {c('skip')}, {c('volume')} 0-100"
        await self.reply(m, text)

    # ── модератори та стрімер (скіп і гучність — ще й помічники) ──
    async def _deny(self, m: ChatMessage) -> None:
        await self.reply(m, "це можуть лише модератори та стрімер 😌")

    async def _deny_control(self, m: ChatMessage) -> None:
        await self.reply(m, "це можуть лише модератори, стрімер і помічники 😌")

    async def cmd_skip(self, m: ChatMessage, arg: str) -> None:
        if not self.can_control(m):
            return await self._deny_control(m)
        t = await self.jb.skip()
        if t:
            await self.reply(m, f"⏭ скіпнуто {t.label}")
        else:
            await self.reply(m, "зараз нічого не грає")

    async def cmd_volume(self, m: ChatMessage, arg: str) -> None:
        if not self.can_control(m):
            return await self._deny_control(m)
        if not arg:
            return await self.reply(m, f"🔊 гучність: {self.jb.player.volume}")
        try:
            v = int(arg.rstrip("%"))
        except ValueError:
            return await self.reply(m, f"формат: {self.first('volume')} 0-100")
        await self.jb.set_volume(v)
        await self.reply(m, f"🔊 гучність: {self.jb.player.volume}")

    async def cmd_pause(self, m: ChatMessage, arg: str) -> None:
        if not self.privileged(m):
            return await self._deny(m)
        if not self.jb.now:
            return await self.reply(m, "зараз нічого не грає")
        await self.jb.set_pause(not self.jb.player.paused)
        await self.reply(m, "⏸ пауза" if self.jb.player.paused else "▶ граємо далі")

    async def cmd_play(self, m: ChatMessage, arg: str) -> None:
        if not self.privileged(m):
            return await self._deny(m)
        if not self.jb.active:
            self.jb.activate()
            return await self.reply(m, "▶ музику ввімкнено")
        if self.jb.player.paused:
            await self.jb.set_pause(False)
        await self.reply(m, "▶ граємо")

    async def cmd_ban(self, m: ChatMessage, arg: str) -> None:
        if not self.privileged(m):
            return await self._deny(m)
        t = await self.jb.ban_current()
        if t:
            await self.reply(m, f"🚫 {t.label} більше не гратиме на каналі")
        else:
            await self.reply(m, "зараз нічого не грає")
