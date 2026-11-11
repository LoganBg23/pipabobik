"""Завантаження config.toml з типовими значеннями."""
from __future__ import annotations

import copy
import sys
from dataclasses import dataclass, field
from pathlib import Path

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover
    import tomli as tomllib

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
CONFIG_PATH = ROOT / "config.toml"
EXAMPLE_PATH = ROOT / "config.example.toml"

DEFAULTS: dict = {
    "twitch": {"channel": "", "client_id": "", "client_secret": "", "admins": []},
    "google": {
        "cookies_from_browser": "firefox",
        "cookies_file": "",
        "fallback_playlist": "LM",
        "fallback_shuffle": True,
    },
    "player": {"mpv_path": "mpv", "volume": 60, "normalize": True, "audio_device": "", "show_window": True},
    "rules": {
        "who_can_request": "all",
        "max_duration": 420,
        "max_per_user": 2,
        "cooldown": 30,
        "max_queue": 30,
        "only_songs": True,
        "block_explicit": False,
        "banned_words": [],
        "interrupt_fallback": True,
        "block_russian": True,
        "russian_artists": [],
    },
    "commands": {
        "request": ["!трек", "!sr"],
        "now": ["!шоце", "!song"],
        "skip": ["!хуйня", "!skip"],
        "queue": ["!черга", "!queue"],
        "wrong": ["!нетой", "!wrongsong"],
        "volume": ["!гучність", "!vol"],
        "pause": ["!пауза", "!pause"],
        "play": ["!грай", "!play"],
        "ban": ["!бантрек"],
        "help": ["!музика", "!pipabobik"],
    },
    "chat": {"announce_tracks": False, "greet_on_stream_start": True},
    "server": {"port": 17777},
}


def _merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


@dataclass
class Config:
    raw: dict = field(default_factory=lambda: copy.deepcopy(DEFAULTS))
    path: Path = CONFIG_PATH

    def __getitem__(self, key: str) -> dict:
        return self.raw[key]

    @property
    def port(self) -> int:
        return int(self.raw["server"]["port"])

    @property
    def channel(self) -> str:
        return str(self.raw["twitch"]["channel"]).strip().lstrip("#").lower()


def load_config(path: Path = CONFIG_PATH) -> Config:
    data: dict = {}
    if path.exists():
        with open(path, "rb") as f:
            data = tomllib.load(f)
    cfg = Config(raw=_merge(DEFAULTS, data), path=path)
    # нормалізуємо команди: завжди список, нижній регістр
    for k, v in cfg.raw["commands"].items():
        if isinstance(v, str):
            v = [v]
        cfg.raw["commands"][k] = [str(c).strip().lower() for c in v if str(c).strip()]
    cfg.raw["twitch"]["admins"] = [a.lower().lstrip("@") for a in cfg.raw["twitch"]["admins"]]
    cfg.raw["rules"]["banned_words"] = [w.lower() for w in cfg.raw["rules"]["banned_words"]]
    return cfg


def _toml_value(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, list):
        return "[" + ", ".join(_toml_value(x) for x in v) + "]"
    s = str(v).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{s}"'


def set_values(path: Path, updates: dict[str, dict]) -> None:
    """Оновлює значення у config.toml, зберігаючи коментарі.

    Якщо файлу немає — копіює config.example.toml.
    """
    if not path.exists():
        path.write_text(EXAMPLE_PATH.read_text(encoding="utf-8"), encoding="utf-8")
    lines = path.read_text(encoding="utf-8").splitlines()
    section = None
    headers: dict[str, int] = {}
    pending = {s: dict(v) for s, v in updates.items()}
    for i, line in enumerate(lines):
        st = line.strip()
        if st.startswith("[") and st.endswith("]"):
            section = st[1:-1].strip()
            headers[section] = i
            continue
        if section in pending and "=" in st and not st.startswith("#"):
            key = st.split("=", 1)[0].strip()
            if key in pending[section]:
                lines[i] = f"{key} = {_toml_value(pending[section].pop(key))}"
    # ключі, яких не було у файлі: вставляємо одразу після заголовка секції
    for s, kv in sorted(pending.items(), key=lambda x: -headers.get(x[0], -1)):
        if not kv:
            continue
        new = [f"{k} = {_toml_value(v)}" for k, v in kv.items()]
        if s in headers:
            idx = headers[s] + 1
            lines[idx:idx] = new
        else:
            lines += ["", f"[{s}]", *new]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
