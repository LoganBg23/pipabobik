"""Черга, правила замовлень і цикл відтворення."""
from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import deque
from pathlib import Path
from typing import Awaitable, Callable

from .music import MusicSource, Track, fmt_time
from .player import Player

log = logging.getLogger("pipabobik.jukebox")

Notify = Callable[[str], Awaitable[None]]


class Jukebox:
    def __init__(self, rules: dict, google: dict, music: MusicSource, player: Player, data_dir: Path):
        self.rules = rules
        self.google = google
        self.music = music
        self.player = player
        self.data_dir = data_dir
        self.queue: list[Track] = []
        self.now: Track | None = None
        self.loading = False
        self.active = False
        self.history: deque[Track] = deque(maxlen=20)
        self.banned_ids: set[str] = set()
        self.last_request: dict[str, float] = {}
        self._fallback: list[Track] = []
        self._fallback_loaded_at = 0.0
        self._wake = asyncio.Event()
        self._activated = asyncio.Event()
        self._skip_flag = False
        self.listeners: list[Callable[[], None]] = []
        self.say: Notify | None = None
        self.announce = False
        self._load()

    # ── збереження ──
    @property
    def _queue_file(self) -> Path:
        return self.data_dir / "queue.json"

    @property
    def _ban_file(self) -> Path:
        return self.data_dir / "banned.json"

    def _load(self) -> None:
        try:
            if self._queue_file.exists():
                self.queue = [Track.from_dict(d) for d in json.loads(self._queue_file.read_text(encoding="utf-8"))]
                if self.queue:
                    log.info("Відновлено чергу: %d треків", len(self.queue))
            if self._ban_file.exists():
                self.banned_ids = set(json.loads(self._ban_file.read_text(encoding="utf-8")))
        except (OSError, ValueError, TypeError) as e:
            log.warning("Не вдалося відновити дані: %s", e)

    def save(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        items = list(self.queue)
        if self.now and self.now.source == "request" and self.now not in items:
            items.insert(0, self.now)  # якщо бот закривають посеред треку — не губимо його
        self._queue_file.write_text(json.dumps([t.to_dict() for t in items], ensure_ascii=False), encoding="utf-8")
        self._ban_file.write_text(json.dumps(sorted(self.banned_ids)), encoding="utf-8")

    def changed(self) -> None:
        for cb in list(self.listeners):
            try:
                cb()
            except Exception:  # noqa: BLE001
                log.exception("listener")

    # ── правила ──
    def check(self, track: Track, user_id: str = "", privileged: bool = False) -> str | None:
        """Повертає причину відмови або None, якщо трек можна додати."""
        r = self.rules
        if track.video_id in self.banned_ids:
            return f"{track.label} у бан-листі каналу 🚫"
        text = f"{track.title} {track.artist}".lower()
        for w in r.get("banned_words", []):
            if w and w in text:
                return "цей трек заборонений на каналі 🚫"
        if r.get("block_explicit") and track.explicit:
            return f"{track.label} позначений як Explicit — на цьому каналі такі не граємо"
        max_d = int(r.get("max_duration", 0) or 0)
        if max_d and track.duration > max_d and not privileged:
            return f"{track.label} задовгий ({fmt_time(track.duration)}), максимум {fmt_time(max_d)}"
        if (self.now and self.now.video_id == track.video_id) or any(t.video_id == track.video_id for t in self.queue):
            return f"{track.label} вже є в черзі 😉"
        return None

    def check_user(self, user_id: str, privileged: bool) -> str | None:
        if privileged:
            return None
        r = self.rules
        if len(self.queue) >= int(r.get("max_queue", 30)):
            return "черга переповнена, спробуй трохи згодом 🙏"
        per_user = int(r.get("max_per_user", 0) or 0)
        if per_user and sum(1 for t in self.queue if t.requested_by_id == user_id) >= per_user:
            return f"у тебе вже {per_user} трек(и) в черзі — дочекайся їх 🎧"
        cd = int(r.get("cooldown", 0) or 0)
        left = cd - (time.time() - self.last_request.get(user_id, 0))
        if cd and left > 0:
            return f"зачекай ще {int(left) + 1} с перед наступним замовленням ⏳"
        return None

    def add(self, track: Track, user_id: str, login: str) -> int:
        track.requested_by, track.requested_by_id, track.source = login, user_id, "request"
        track.added_at = time.time()
        self.queue.append(track)
        self.last_request[user_id] = time.time()
        pos = len(self.queue)
        # замовлення перебиває фонову музику
        if self.now and self.now.source == "fallback" and self.rules.get("interrupt_fallback", True):
            asyncio.create_task(self.skip())
        self._wake.set()
        self.save()
        self.changed()
        return pos

    def eta(self, position: int) -> int:
        """Скільки секунд до початку треку на позиції position (1-based)."""
        sec = 0
        if self.now and not (self.now.source == "fallback" and self.rules.get("interrupt_fallback", True)):
            sec += max(0, (self.now.duration or 180) - int(self.player.position))
        for t in self.queue[: position - 1]:
            sec += t.duration or 180
        return sec

    def remove_last_by(self, user_id: str) -> Track | None:
        for i in range(len(self.queue) - 1, -1, -1):
            if self.queue[i].requested_by_id == user_id:
                t = self.queue.pop(i)
                self.last_request.pop(user_id, None)
                self.save()
                self.changed()
                return t
        return None

    def remove_at(self, index: int) -> Track | None:
        if 0 <= index < len(self.queue):
            t = self.queue.pop(index)
            self.save()
            self.changed()
            return t
        return None

    def move_up(self, index: int) -> None:
        if 0 < index < len(self.queue):
            self.queue[index - 1], self.queue[index] = self.queue[index], self.queue[index - 1]
            self.save()
            self.changed()

    # ── керування ──
    async def skip(self) -> Track | None:
        cur = self.now
        self._skip_flag = True
        await self.player.stop()
        return cur

    async def ban_current(self) -> Track | None:
        cur = self.now
        if cur:
            self.banned_ids.add(cur.video_id)
            self.save()
            await self.skip()
        return cur

    async def set_pause(self, paused: bool) -> None:
        await self.player.set_pause(paused)
        self.changed()

    async def set_volume(self, vol: int) -> None:
        await self.player.set_volume(vol)
        self.changed()

    def activate(self) -> None:
        if not self.active:
            log.info("Музику ввімкнено")
        self.active = True
        self._activated.set()
        self._wake.set()
        self.changed()

    async def deactivate(self) -> None:
        if self.active:
            log.info("Музику вимкнено (стрім завершено)")
        self.active = False
        self._activated.clear()
        cur = self.now
        if cur and cur.source == "request" and cur not in self.queue:
            self.queue.insert(0, cur)  # дограємо наступного разу
        self.now = None
        await self.player.stop()
        self.save()
        self.changed()

    # ── стан для віджета ──
    def state(self) -> dict:
        now = None
        if self.now:
            now = self.now.to_dict()
            now["elapsed"] = round(self.player.position, 2)
            now["url"] = self.now.url
        if not self.active:
            status = "offline"
        elif self.loading:
            status = "loading"
        elif self.now and self.player.paused:
            status = "paused"
        elif self.now:
            status = "playing"
        else:
            status = "idle"
        return {
            "status": status,
            "now": now,
            "queue": [t.to_dict() for t in self.queue[:15]],
            "queue_len": len(self.queue),
            "volume": self.player.volume,
            "active": self.active,
        }

    # ── фоновий плейлист ──
    async def _next_fallback(self) -> Track | None:
        pl = (self.google.get("fallback_playlist") or "").strip()
        if not pl:
            return None
        if not self._fallback and time.time() - self._fallback_loaded_at > 300:
            self._fallback_loaded_at = time.time()
            self._fallback = await self.music.playlist(pl, bool(self.google.get("fallback_shuffle", True)))
        while self._fallback:
            t = self._fallback.pop(0)
            if t.video_id not in self.banned_ids:
                return t
        return None

    # ── головний цикл ──
    async def run(self) -> None:
        while True:
            if not self.active:
                await self._activated.wait()
                continue
            self._skip_flag = False
            if self.queue:
                track = self.queue.pop(0)
                self.save()
            else:
                self._wake.clear()
                track = await self._next_fallback()
                if track is None:
                    self.now = None
                    self.changed()
                    await self._wake.wait()
                    continue
                if self.queue:  # поки вантажився плейлист, хтось замовив трек
                    self._fallback.insert(0, track)
                    continue
            await self._play_one(track)

    async def _play_one(self, track: Track) -> None:
        self.now, self.loading = track, True
        self.changed()
        try:
            res = await self.music.resolve_stream(track)
        finally:
            self.loading = False
        if not self.active or self._skip_flag:
            if self.now is track:
                self.now = None
                self.changed()
            return
        if not res:
            log.warning("Не вдалося отримати аудіо для %s", track.label)
            if track.source == "request" and self.say:
                await self.say(f"@{track.requested_by} не вдалося відтворити {track.label} (недоступний у регіоні чи видалений) — пропускаю 😔")
            self.now = None
            await asyncio.sleep(1)
            return
        self.changed()
        if self.announce and self.say:
            who = f" · замовив(ла) @{track.requested_by}" if track.source == "request" else ""
            await self.say(f"🎶 Грає {track.label}{who}")
        log.info("▶ %s [%s]", track.label, track.source)
        url, headers = res
        outcome = await self.player.play(url, headers)
        if outcome == "error" and track.source == "request" and self.say:
            await self.say(f"@{track.requested_by} {track.label} не відтворився — пропускаю 😔")
        if outcome != "stopped" or self._skip_flag:
            self.history.append(track)
        if self.now is track:
            self.now = None
        self.changed()
