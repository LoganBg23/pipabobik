"""Пошук треків у YouTube Music та отримання потоку через yt-dlp (з логіном Google)."""
from __future__ import annotations

import asyncio
import logging
import random
import re
import time
from dataclasses import asdict, dataclass, field
from functools import partial
from pathlib import Path

log = logging.getLogger("pipabobik.music")

VIDEO_ID_RE = re.compile(
    r"(?:youtube\.com/(?:watch\?(?:.*&)?v=|shorts/|embed/|live/)|youtu\.be/|music\.youtube\.com/watch\?(?:.*&)?v=)"
    r"([A-Za-z0-9_-]{11})"
)
PLAYLIST_RE = re.compile(r"[?&]list=([A-Za-z0-9_-]+)")


@dataclass
class Track:
    video_id: str
    title: str
    artist: str = ""
    album: str = ""
    duration: int = 0
    cover: str = ""
    explicit: bool = False
    requested_by: str = ""
    requested_by_id: str = ""
    source: str = "request"  # request | fallback
    added_at: float = field(default_factory=time.time)

    @property
    def url(self) -> str:
        return f"https://music.youtube.com/watch?v={self.video_id}"

    @property
    def label(self) -> str:
        return f"«{self.title}» — {self.artist}" if self.artist else f"«{self.title}»"

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Track":
        known = {k: v for k, v in d.items() if k in cls.__dataclass_fields__}
        return cls(**known)


class _YdlLogger:
    """Перенаправляє повідомлення yt-dlp у наш лог замість консолі."""

    def debug(self, msg: str) -> None:
        pass

    def info(self, msg: str) -> None:
        pass

    def warning(self, msg: str) -> None:
        log.debug("yt-dlp: %s", msg)

    def error(self, msg: str) -> None:
        log.debug("yt-dlp: %s", msg)


def fmt_time(sec: float) -> str:
    sec = max(0, int(sec))
    return f"{sec // 60}:{sec % 60:02d}"


def best_cover(thumbnails: list[dict] | None, video_id: str = "") -> str:
    """Обирає найбільшу обкладинку; для googleusercontent підвищує розмір до 544px."""
    if thumbnails:
        url = max(thumbnails, key=lambda t: t.get("width", 0) * t.get("height", 0)).get("url", "")
        if "googleusercontent.com" in url or "ggpht.com" in url:
            url = re.sub(r"=w\d+-h\d+", "=w544-h544", url)
        if url:
            return url
    return f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg" if video_id else ""


def extract_video_id(text: str) -> str | None:
    m = VIDEO_ID_RE.search(text)
    return m.group(1) if m else None


def _artists(item: dict) -> str:
    names = [a.get("name", "") for a in item.get("artists") or [] if a.get("name")]
    return ", ".join(names)


class MusicSource:
    """Обгортка над ytmusicapi (пошук) та yt-dlp (потік, плейлисти)."""

    def __init__(self, cookies_from_browser: str = "", cookies_file: str = ""):
        self.cookies_from_browser = (cookies_from_browser or "").strip().lower()
        self.cookies_file = (cookies_file or "").strip()
        self._ytm = None
        self._cookies_broken = False

    # ── ytmusicapi ──
    @property
    def ytm(self):
        if self._ytm is None:
            from ytmusicapi import YTMusic

            # українська локаль у ytmusicapi відсутня; англійська — найнадійніша для парсингу.
            # location=UA — щоб видача відповідала українському регіону.
            self._ytm = YTMusic(language="en", location="UA")
        return self._ytm

    def _search_sync(self, query: str, only_songs: bool, limit: int = 8) -> list[Track]:
        filters = ["songs"] if only_songs else ["songs", "videos"]
        tracks: list[Track] = []
        for flt in filters:
            try:
                results = self.ytm.search(query, filter=flt, limit=limit)
            except Exception as e:  # noqa: BLE001 — ytmusicapi кидає різне
                log.warning("Пошук YT Music (%s) не вдався: %s", flt, e)
                continue
            for it in results:
                vid = it.get("videoId")
                if not vid:
                    continue
                tracks.append(
                    Track(
                        video_id=vid,
                        title=it.get("title") or "Без назви",
                        artist=_artists(it),
                        album=(it.get("album") or {}).get("name", "") if isinstance(it.get("album"), dict) else "",
                        duration=int(it.get("duration_seconds") or 0),
                        cover=best_cover(it.get("thumbnails"), vid),
                        explicit=bool(it.get("isExplicit")),
                    )
                )
            if tracks:
                break
        return tracks

    async def search(self, query: str, only_songs: bool = True) -> list[Track]:
        return await asyncio.to_thread(self._search_sync, query, only_songs)

    def _by_id_sync(self, video_id: str) -> Track | None:
        try:
            song = self.ytm.get_song(video_id)
            vd = song.get("videoDetails") or {}
            if vd.get("videoId"):
                return Track(
                    video_id=video_id,
                    title=vd.get("title") or "Без назви",
                    artist=(vd.get("author") or "").removesuffix(" - Topic"),
                    duration=int(vd.get("lengthSeconds") or 0),
                    cover=best_cover((vd.get("thumbnail") or {}).get("thumbnails"), video_id),
                )
        except Exception as e:  # noqa: BLE001
            log.info("get_song(%s) не вдався: %s — пробую yt-dlp", video_id, e)
        info = self._ydl_info(f"https://www.youtube.com/watch?v={video_id}", flat=False, stream=False)
        if not info:
            return None
        return Track(
            video_id=video_id,
            title=info.get("track") or info.get("title") or "Без назви",
            artist=info.get("artist") or info.get("uploader") or "",
            duration=int(info.get("duration") or 0),
            cover=best_cover(info.get("thumbnails"), video_id),
        )

    async def by_id(self, video_id: str) -> Track | None:
        return await asyncio.to_thread(self._by_id_sync, video_id)

    # ── yt-dlp ──
    def ydl_opts(self) -> dict:
        opts: dict = {
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
            "format": "bestaudio[acodec=opus]/bestaudio/best",
            "socket_timeout": 15,
            "logger": _YdlLogger(),
        }
        if self._cookies_broken:
            return opts
        if self.cookies_file and Path(self.cookies_file).exists():
            opts["cookiefile"] = self.cookies_file
        elif self.cookies_from_browser:
            opts["cookiesfrombrowser"] = (self.cookies_from_browser, None, None, None)
        return opts

    def _ydl_info(self, url: str, flat: bool, stream: bool = True) -> dict | None:
        import yt_dlp

        opts = self.ydl_opts()
        if flat:
            opts.update({"extract_flat": "in_playlist", "noplaylist": False})
            opts.pop("format", None)
        if not stream and not flat:
            opts.pop("format", None)
            opts["skip_download"] = True
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                return ydl.extract_info(url, download=False)
        except Exception as e:  # noqa: BLE001
            text = str(e)
            has_cookies = "cookiefile" in opts or "cookiesfrombrowser" in opts
            if has_cookies and ("cookie" in text.lower() or "firefox" in text.lower()):
                # логін недоступний (Firefox не встановлено / профіль заблоковано) — граємо без нього
                log.warning("Логін Google недоступний (%s) — працюю без нього", text.splitlines()[0][:200])
                self._cookies_broken = True
                return self._ydl_info(url, flat, stream)
            log.warning("yt-dlp: %s", text.splitlines()[0][:300])
            return None

    async def resolve_stream(self, track: Track) -> tuple[str, dict] | None:
        """Повертає (пряме посилання на аудіо, http-заголовки) або None."""
        info = await asyncio.to_thread(partial(self._ydl_info, track.url, False))
        if not info:
            return None
        url = info.get("url")
        if not url and info.get("requested_formats"):
            url = info["requested_formats"][0].get("url")
        if not url:
            return None
        if not track.duration and info.get("duration"):
            track.duration = int(info["duration"])
        return url, dict(info.get("http_headers") or {})

    async def playlist(self, playlist: str, shuffle: bool = True) -> list[Track]:
        if not playlist:
            return []
        m = PLAYLIST_RE.search(playlist)
        pid = m.group(1) if m else playlist.strip()
        url = f"https://music.youtube.com/playlist?list={pid}"
        info = await asyncio.to_thread(partial(self._ydl_info, url, True))
        if not info:
            return []
        out = []
        for e in info.get("entries") or []:
            vid = e.get("id")
            if not vid or len(vid) != 11:
                continue
            out.append(
                Track(
                    video_id=vid,
                    title=e.get("title") or "Без назви",
                    artist=(e.get("uploader") or e.get("channel") or "").removesuffix(" - Topic"),
                    duration=int(e.get("duration") or 0),
                    cover=best_cover(e.get("thumbnails"), vid),
                    source="fallback",
                )
            )
        if shuffle:
            random.shuffle(out)
        log.info("Фоновий плейлист: %d треків", len(out))
        return out
