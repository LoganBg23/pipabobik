"""Команди чату."""
from __future__ import annotations

import logging
from typing import Awaitable, Callable

from .access import Access
from .jukebox import Jukebox
from .music import MusicSource, Track, extract_video_id, fmt_time
from .twitch import ChatMessage

log = logging.getLogger("pipabobik.commands")

Say = Callable[[str, str | None], Awaitable[None]]


def parse_seek(arg: str, current: float) -> float | None:
    """«1:30», «90» — абсолютна позиція; «+30», «-1:00» — зсув від поточної."""
    a = arg.strip().replace(",", ".").replace(" ", "")
    if not a:
        return None
    sign = 0
    if a[0] in "+-":
        sign = 1 if a[0] == "+" else -1
        a = a[1:]
    try:
        parts = [float(x) for x in a.split(":")]
    except ValueError:
        return None
    if not parts or len(parts) > 3 or any(x < 0 for x in parts):
        return None
    sec = 0.0
    for x in parts:
        sec = sec * 60 + x
    return current + sign * sec if sign else sec


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
        self.access = Access(cfg)
        self.handlers: dict[str, Callable[[ChatMessage, str], Awaitable[None]]] = {
            "request": self.cmd_request,
            "now": self.cmd_now,
            "skip": self.cmd_skip,
            "queue": self.cmd_queue,
            "wrong": self.cmd_wrong,
            "volume": self.cmd_volume,
            "seek": self.cmd_seek,
            "pause": self.cmd_pause,
            "play": self.cmd_play,
            "ban": self.cmd_ban,
            "help": self.cmd_help,
        }
        self.table: dict[str, tuple[str, Callable[[ChatMessage, str], Awaitable[None]]]] = {}
        self.rebuild()

    def rebuild(self) -> None:
        """Перечитує назви команд (після змін у застосунку)."""
        self.table = {}
        for key, fn in self.handlers.items():
            for alias in self.cfg["commands"].get(key, []):
                self.table.setdefault(alias, (key, fn))

    def first(self, key: str) -> str:
        names = self.cfg["commands"].get(key) or [f"!{key}"]
        return names[0]

    def privileged(self, m: ChatMessage) -> bool:
        """Модератор, стрімерка або адмін з config.toml."""
        return m.is_broadcaster or "mods" in self.access.roles_of(m)

    def can(self, m: ChatMessage, key: str) -> bool:
        return self.access.can(m, key)

    async def reply(self, m: ChatMessage, text: str) -> None:
        await self.say(text, m.message_id)

    async def handle(self, m: ChatMessage) -> None:
        if not m.text.startswith("!"):
            return
        parts = m.text.split(maxsplit=1)
        name = parts[0].lower()
        arg = parts[1].strip() if len(parts) > 1 else ""
        entry = self.table.get(name)
        if not entry:
            return
        key, fn = entry
        log.info("%s: %s", m.login, m.text)
        if not self.can(m, key):
            return await self._deny(m, key)
        await fn(m, arg)

    # ── для всіх ──
    async def cmd_request(self, m: ChatMessage, arg: str) -> None:
        if not arg:
            return await self.reply(m, f"напиши назву: {self.first('request')} виконавець — пісня 🎵")
        lim = self.access.limits(m)
        reason = self.jb.check_user(m.user_id, limits=lim)
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
            r = self.jb.check(t, m.user_id, limits=lim)
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
        reason = self.jb.check_user(m.user_id, limits=lim)
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
        hints = {
            "request": f"{c('request')} назва — замовити",
            "now": f"{c('now')} — що грає",
            "queue": f"{c('queue')} — черга",
            "wrong": f"{c('wrong')} — прибрати свій трек",
            "skip": f"{c('skip')} — пропустити",
            "volume": f"{c('volume')} 0-100",
            "seek": f"{c('seek')} 1:30 або +30",
            "pause": f"{c('pause')}",
            "play": f"{c('play')}",
            "ban": f"{c('ban')} — заборонити трек",
        }
        parts = [h for k, h in hints.items() if self.cfg["commands"].get(k) and self.can(m, k)]
        await self.reply(m, "🎵 pipabobik: " + (" · ".join(parts) if parts else "тобі зараз доступна лише ця підказка"))

    # ── керування (хто саме може — налаштовується у вкладці «Команди») ──
    async def _deny(self, m: ChatMessage, key: str) -> None:
        allowed = self.access.roles_for(key)
        if key == "request":
            return await self.reply(m, "замовлення зараз доступні не всім глядачам 🙈")
        names = {"subs": "саби", "vip": "VIP", "helpers": "помічники", "mods": "модератори"}
        who = ", ".join(names[r] for r in allowed if r in names)
        await self.reply(m, f"це можуть лише {who + ' і ' if who else ''}стрімер 😌")

    async def cmd_skip(self, m: ChatMessage, arg: str) -> None:
        t = await self.jb.skip(f"команда {m.text.split()[0]} від {m.login}")
        if t:
            await self.reply(m, f"⏭ скіпнуто {t.label}")
        else:
            await self.reply(m, "зараз нічого не грає")

    async def cmd_volume(self, m: ChatMessage, arg: str) -> None:
        if not arg:
            return await self.reply(m, f"🔊 гучність: {self.jb.player.volume}")
        try:
            v = int(arg.rstrip("%"))
        except ValueError:
            return await self.reply(m, f"формат: {self.first('volume')} 0-100")
        await self.jb.set_volume(v)
        await self.reply(m, f"🔊 гучність: {self.jb.player.volume}")

    async def cmd_seek(self, m: ChatMessage, arg: str) -> None:
        t = self.jb.now
        if not t:
            return await self.reply(m, "зараз нічого не грає")
        target = parse_seek(arg, self.jb.player.position)
        if target is None:
            return await self.reply(m, f"формат: {self.first('seek')} 1:30 (на позицію) або +30 / -15 (секунд)")
        pos = await self.jb.seek(target)
        if pos is None:
            return await self.reply(m, "не вдалося перемотати 😕")
        await self.reply(m, f"⏩ {t.label}: {fmt_time(int(pos))}" + (f" / {fmt_time(t.duration)}" if t.duration else ""))

    async def cmd_pause(self, m: ChatMessage, arg: str) -> None:
        if not self.jb.now:
            return await self.reply(m, "зараз нічого не грає")
        await self.jb.set_pause(not self.jb.player.paused)
        await self.reply(m, "⏸ пауза" if self.jb.player.paused else "▶ граємо далі")

    async def cmd_play(self, m: ChatMessage, arg: str) -> None:
        if not self.jb.active:
            self.jb.activate()
            return await self.reply(m, "▶ музику ввімкнено")
        if self.jb.player.paused:
            await self.jb.set_pause(False)
        await self.reply(m, "▶ граємо")

    async def cmd_ban(self, m: ChatMessage, arg: str) -> None:
        t = await self.jb.ban_current()
        if t:
            await self.reply(m, f"🚫 {t.label} більше не гратиме на каналі")
        else:
            await self.reply(m, "зараз нічого не грає")
