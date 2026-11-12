"""Список фонових плейлистів, між якими стрімерка перемикається в застосунку.

Зберігається в data/playlists.json. Який із них грає зараз — google.fallback_playlist у config.toml
(порожньо — тиша, коли черга порожня).
"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path

log = logging.getLogger("pipabobik.playlists")

LIKED = "LM"  # «Вподобані» у вашому YouTube Music
_ID = re.compile(r"^[A-Za-z0-9_-]{2,80}$")
_LIST = re.compile(r"[?&]list=([A-Za-z0-9_-]+)")
_BROWSE = re.compile(r"/browse/VL([A-Za-z0-9_-]+)")


def playlist_id(text: str) -> str | None:
    """Ідентифікатор плейлиста з посилання YouTube / YouTube Music або сам ідентифікатор."""
    t = (text or "").strip()
    if not t:
        return None
    if t.upper() == LIKED:
        return LIKED
    m = _LIST.search(t) or _BROWSE.search(t)
    if m:
        return m.group(1)
    if t.startswith("VL") and _ID.match(t[2:]):
        t = t[2:]
    return t if _ID.match(t) and "/" not in t else None


class Playlists:
    def __init__(self, path: Path, active: str = ""):
        self.path = path
        self.items: list[dict] = []
        self._load(active)

    def _load(self, active: str) -> None:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            self.items = [{"id": str(x["id"]), "name": str(x.get("name") or x["id"])} for x in data if x.get("id")]
        except (OSError, ValueError, TypeError, KeyError):
            self.items = []
        if not self.items:
            # перший запуск: «Вподобані» і плейлист, який уже був у config.toml
            self.items = [{"id": LIKED, "name": "Вподобані"}]
            pid = playlist_id(active)
            if pid and pid != LIKED:
                self.items.append({"id": pid, "name": "Мій плейлист"})
            self.save()

    def save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(self.items, ensure_ascii=False, indent=1), encoding="utf-8")
        except OSError as e:
            log.warning("Не вдалося зберегти плейлисти: %s", e)

    def get(self, pid: str) -> dict | None:
        return next((x for x in self.items if x["id"] == pid), None)

    def add(self, pid: str, name: str) -> dict:
        found = self.get(pid)
        if found:
            if name:
                found["name"] = name
        else:
            found = {"id": pid, "name": name or "Плейлист"}
            self.items.append(found)
        self.save()
        return found

    def remove(self, pid: str) -> None:
        self.items = [x for x in self.items if x["id"] != pid]
        self.save()
