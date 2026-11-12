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

RESUME_ATTEMPTS = 2  # скільки разів продовжувати трек, який YouTube обірвав посередині
SKIP_GUARD = 3.0  # с: повторний «скіп» без адресата одразу після попереднього не чіпає наступний трек
STREAM_TTL = 20 * 60  # с: скільки тримати наперед отримане посилання на аудіо
PREFETCH_AHEAD = 2  # скільки наступних треків готувати заздалегідь (на випадок кількох скіпів поспіль)

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
        self.banned_meta: dict[str, str] = {}
        self.allowed_ids: set[str] = set()  # розбанені вручну — фільтр їх більше не чіпає
        self.liked_ids: set[str] = set()  # лайкнуті з панелі в YouTube Music
        # async (track) -> причина або None; задається ботом (фільтр російської музики)
        self.content_filter: Callable[[Track], Awaitable[str | None]] | None = None
        self.last_request: dict[str, float] = {}
        self._fallback: list[Track] = []
        self._fallback_loaded_at = 0.0
        self._wake = asyncio.Event()
        self._activated = asyncio.Event()
        # який саме трек треба пропустити: так запізнілий «скіп» не зачепить наступний трек
        self._skip_target: Track | None = None
        self._last_skip_at = 0.0
        # наперед отримані посилання на аудіо: після скіпу наступний трек вмикається одразу
        self._streams: dict[str, tuple[tuple[str, dict], float]] = {}
        self._stream_tasks: dict[str, asyncio.Task] = {}
        self._prefetch_task: asyncio.Task | None = None
        self._fallback_gen = 0  # змінюється при перемиканні фонового плейлиста
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

    @property
    def _allow_file(self) -> Path:
        return self.data_dir / "allowed.json"

    @property
    def _liked_file(self) -> Path:
        return self.data_dir / "liked.json"

    def _load(self) -> None:
        try:
            if self._queue_file.exists():
                self.queue = [Track.from_dict(d) for d in json.loads(self._queue_file.read_text(encoding="utf-8"))]
                if self.queue:
                    log.info("Відновлено чергу: %d треків", len(self.queue))
            if self._ban_file.exists():
                data = json.loads(self._ban_file.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    self.banned_meta = data
                else:  # старий формат — просто список id
                    self.banned_meta = {vid: vid for vid in data}
                self.banned_ids = set(self.banned_meta)
            if self._allow_file.exists():
                self.allowed_ids = set(json.loads(self._allow_file.read_text(encoding="utf-8")))
            if self._liked_file.exists():
                self.liked_ids = set(json.loads(self._liked_file.read_text(encoding="utf-8")))
        except (OSError, ValueError, TypeError) as e:
            log.warning("Не вдалося відновити дані: %s", e)

    def save(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        items = list(self.queue)
        if self.now and self.now.source == "request" and self.now not in items:
            items.insert(0, self.now)  # якщо бот закривають посеред треку — не губимо його
        self._queue_file.write_text(json.dumps([t.to_dict() for t in items], ensure_ascii=False), encoding="utf-8")
        meta = {vid: self.banned_meta.get(vid, vid) for vid in sorted(self.banned_ids)}
        self._ban_file.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
        self._allow_file.write_text(json.dumps(sorted(self.allowed_ids)), encoding="utf-8")
        self._liked_file.write_text(json.dumps(sorted(self.liked_ids)), encoding="utf-8")

    def changed(self) -> None:
        for cb in list(self.listeners):
            try:
                cb()
            except Exception:  # noqa: BLE001
                log.exception("listener")

    # ── правила ──
    def check(self, track: Track, user_id: str = "", privileged: bool = False, limits: dict | None = None) -> str | None:
        """Повертає причину відмови або None, якщо трек можна додати.

        limits — ліміти ролі глядача (див. access.py); без них діють загальні правила.
        """
        r = self.rules
        if track.video_id in self.banned_ids:
            return f"{track.label} у бан-листі каналу 🚫"
        text = f"{track.title} {track.artist}".lower()
        for w in r.get("banned_words", []):
            if w and w in text:
                return "цей трек заборонений на каналі 🚫"
        if r.get("block_explicit") and track.explicit:
            return f"{track.label} позначений як Explicit — на цьому каналі такі не граємо"
        max_d = int((limits if limits is not None else r).get("max_duration", 0) or 0)
        if max_d and track.duration > max_d and not privileged:
            return f"{track.label} задовгий ({fmt_time(track.duration)}), максимум {fmt_time(max_d)}"
        if (self.now and self.now.video_id == track.video_id) or any(t.video_id == track.video_id for t in self.queue):
            return f"{track.label} вже є в черзі 😉"
        return None

    def check_user(self, user_id: str, privileged: bool = False, limits: dict | None = None) -> str | None:
        if privileged:
            return None
        r = self.rules
        lim = limits if limits is not None else r
        if len(self.queue) >= int(r.get("max_queue", 30)) and not lim.get("bypass_queue"):
            return "черга переповнена, спробуй трохи згодом 🙏"
        per_user = int(lim.get("max_per_user", 0) or 0)
        if per_user and sum(1 for t in self.queue if t.requested_by_id == user_id) >= per_user:
            return f"у тебе вже {per_user} трек(и) в черзі — дочекайся їх 🎧"
        cd = int(lim.get("cooldown", 0) or 0)
        left = cd - (time.time() - self.last_request.get(user_id, 0))
        if cd and left > 0:
            return f"зачекай ще {int(left) + 1} с перед наступним замовленням ⏳"
        return None

    def add(self, track: Track, user_id: str, login: str, front: bool = False) -> int:
        track.requested_by, track.requested_by_id, track.source = login, user_id, "request"
        track.added_at = time.time()
        if front:
            self.queue.insert(0, track)
            pos = 1
        else:
            self.queue.append(track)
            pos = len(self.queue)
        self.last_request[user_id] = time.time()
        # замовлення перебиває фонову музику
        if self.now and self.now.source == "fallback" and self.rules.get("interrupt_fallback", True):
            asyncio.create_task(self.skip("замовлення перебиває фонову музику", only=self.now))
        self._wake.set()
        if pos <= PREFETCH_AHEAD and self.now:
            self.prefetch()  # цей трек скоро заграє — готуємо його заздалегідь
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
        self.move(index, index - 1)

    def move(self, index: int, to: int) -> None:
        if 0 <= index < len(self.queue) and 0 <= to < len(self.queue) and index != to:
            self.queue.insert(to, self.queue.pop(index))
            self.save()
            self.changed()

    def clear(self) -> None:
        self.queue.clear()
        self.save()
        self.changed()

    def unban(self, video_id: str) -> None:
        self.banned_ids.discard(video_id)
        self.banned_meta.pop(video_id, None)
        self.allowed_ids.add(video_id)
        self.save()
        self.changed()

    def ban_track(self, track: Track, reason: str = "") -> None:
        self.banned_ids.add(track.video_id)
        self.banned_meta[track.video_id] = f"{track.label} · {reason}" if reason else track.label
        self.queue = [t for t in self.queue if t.video_id != track.video_id]
        self.save()
        self.changed()

    async def filter_reason(self, track: Track) -> str | None:
        """Автофільтр (російська музика). Розбанені вручну треки пропускає."""
        if not self.content_filter or track.video_id in self.allowed_ids:
            return None
        try:
            return await self.content_filter(track)
        except Exception:  # noqa: BLE001
            log.exception("фільтр")
            return None

    def wake(self) -> None:
        """Розбудити головний цикл (напр. з'явився фоновий плейлист, а до того була тиша)."""
        self._wake.set()

    def reset_fallback(self) -> None:
        self._fallback = []
        self._fallback_loaded_at = 0.0
        self._fallback_gen += 1
        if self._prefetch_task and not self._prefetch_task.done():
            self._prefetch_task.cancel()

    # ── керування ──
    async def skip(self, reason: str = "", only: Track | None = None) -> Track | None:
        """Пропускає поточний трек. only — пропустити лише якщо зараз грає саме він.

        Без only (команда в чаті) повторний скіп протягом SKIP_GUARD секунд ігнорується:
        інакше два «!скіп» поспіль чи подвійне натискання пропускають і наступний трек.
        """
        cur = self.now
        if only is not None and cur is not only:
            return None  # поки чекали, заграв інший трек — його не чіпаємо
        if cur is None:
            return None
        if only is None and time.monotonic() - self._last_skip_at < SKIP_GUARD:
            log.info("Повторний пропуск одразу після попереднього — ігнорую (%s)", reason or "без причини")
            return None
        log.info("⏭ Пропущено %s — %s", cur.label, reason or "без причини")
        self._last_skip_at = time.monotonic()
        self._skip_target = cur
        await self.player.stop()
        return cur

    def recently_skipped(self) -> bool:
        return time.monotonic() - self._last_skip_at < SKIP_GUARD

    # ── посилання на аудіо (з попереднім отриманням) ──
    def _stream_task(self, track: Track) -> asyncio.Task:
        vid = track.video_id
        task = self._stream_tasks.get(vid)
        if task is None:
            task = asyncio.create_task(self.music.resolve_stream(track))
            self._stream_tasks[vid] = task

            def done(t: asyncio.Task, vid=vid) -> None:
                self._stream_tasks.pop(vid, None)
                if not t.cancelled() and t.exception() is None and t.result():
                    self._streams[vid] = (t.result(), time.time())

            task.add_done_callback(done)
        return task

    async def _resolve(self, track: Track):
        """Посилання на аудіо: готове з кешу, те, що вже отримується, або нове."""
        hit = self._streams.pop(track.video_id, None)
        if hit and time.time() - hit[1] < STREAM_TTL:
            return hit[0]
        return await asyncio.shield(self._stream_task(track))

    def prefetch(self) -> None:
        """У фоні готує наступний трек, щоб після скіпу чи кінця треку не чекати."""
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return
        if self._prefetch_task and not self._prefetch_task.done():
            self._prefetch_task.cancel()
        self._prefetch_task = asyncio.create_task(self._prefetch())

    async def _prefetch(self) -> None:
        try:
            await asyncio.sleep(0.2)  # даємо поточному треку спокійно стартувати
            targets = list(self.queue[:PREFETCH_AHEAD])
            if len(targets) < PREFETCH_AHEAD:
                gen = self._fallback_gen
                nxt = await self._next_fallback()
                if nxt is not None and gen == self._fallback_gen:
                    self._fallback.insert(0, nxt)  # повертаємо: заграє наступним
                    targets.append(nxt)
            for vid in [v for v, (_, at) in self._streams.items() if time.time() - at > STREAM_TTL]:
                self._streams.pop(vid, None)
            for t in targets:
                if t.video_id not in self._streams:
                    await asyncio.shield(self._stream_task(t))
        except asyncio.CancelledError:
            pass
        except Exception:  # noqa: BLE001
            log.debug("prefetch", exc_info=True)

    async def ban_current(self) -> Track | None:
        cur = self.now
        if cur:
            self.banned_ids.add(cur.video_id)
            self.banned_meta[cur.video_id] = cur.label
            self.save()
            await self.skip("трек заборонено", only=cur)
        return cur

    async def like(self, video_id: str, like: bool) -> None:
        """Лайкає трек у YouTube Music (через ваш вхід Google) і запам'ятовує це."""
        await self.music.rate(video_id, like)
        if like:
            self.liked_ids.add(video_id)
        else:
            self.liked_ids.discard(video_id)
        self.save()
        self.changed()

    async def set_pause(self, paused: bool) -> None:
        await self.player.set_pause(paused)
        self.changed()

    async def set_volume(self, vol: int) -> None:
        await self.player.set_volume(vol)
        self.changed()

    async def seek(self, position: float) -> float | None:
        """Перемотує поточний трек. Повертає нову позицію або None."""
        if not self.now:
            return None
        pos = max(0.0, float(position))
        if self.now.duration:
            pos = min(pos, max(0.0, self.now.duration - 2))  # не в самий кінець, щоб трек не обірвався
        if not await self.player.seek(pos):
            return None
        self.changed()
        return pos

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
            now["liked"] = self.now.video_id in self.liked_ids
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
            "history": [dict(t.to_dict(), liked=t.video_id in self.liked_ids) for t in list(self.history)[-8:][::-1]],
            "banned": [{"video_id": k, "label": v} for k, v in self.banned_meta.items() if k in self.banned_ids],
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
            if t.video_id in self.banned_ids:
                continue
            reason = await self.filter_reason(t)
            if reason:
                log.info("Фоновий плейлист: пропущено %s (%s)", t.label, reason)
                self.ban_track(t, reason)
                continue
            return t
        return None

    # ── головний цикл ──
    async def run(self) -> None:
        while True:
            if not self.active:
                await self._activated.wait()
                continue
            self._skip_target = None
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
            res = await self._resolve(track)
        finally:
            self.loading = False
        if not self.active or self._skip_target is track:
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
        self.prefetch()
        url, headers = res
        start = 0.0
        for attempt in range(1 + RESUME_ATTEMPTS):
            outcome = await self.player.play(url, headers, start=start)
            played = float(getattr(self.player, "last_played", 0.0) or 0.0)
            if outcome == "stopped" or self._skip_target is track or not self.active:
                break
            # YouTube інколи обриває потік посеред треку: mpv завершується, ніби трек скінчився.
            # Тоді беремо свіже посилання і продовжуємо з того самого місця.
            cut = bool(track.duration) and 3 <= played < track.duration - 15
            if not cut or attempt == RESUME_ATTEMPTS:
                if cut:
                    log.warning("%s обірвався на %s з %s і не відновився", track.label, fmt_time(int(played)), fmt_time(track.duration))
                break
            log.warning("%s обірвався на %s з %s (mpv: %s) — продовжую з того ж місця",
                        track.label, fmt_time(int(played)), fmt_time(track.duration), getattr(self.player, "last_error", "") or "—")
            res = await self.music.resolve_stream(track)
            if not res or self._skip_target is track or not self.active:
                break
            url, headers = res
            start = max(0.0, played - 2)
        if outcome == "error" and track.source == "request" and self.say:
            await self.say(f"@{track.requested_by} {track.label} не відтворився — пропускаю 😔")
        if outcome != "stopped" or self._skip_target is track:
            self.history.append(track)
        if self.now is track:
            self.now = None
        self.changed()
