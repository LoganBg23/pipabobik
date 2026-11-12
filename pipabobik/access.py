"""Хто що може: ролі глядачів, дозволи на команди, ліміти замовлень.

Ролі накопичуються: саб-VIP має і роль «Саби», і роль «VIP», а дозволи й ліміти
беруться найщедріші з усіх його ролей. Стрімерка може все завжди.
"""
from __future__ import annotations

import re

ROLES: list[tuple[str, str]] = [
    ("all", "Усі"),
    ("subs", "Саби"),
    ("vip", "VIP"),
    ("helpers", "Помічники"),
    ("mods", "Модератори"),
]
ROLE_KEYS = [k for k, _ in ROLES]

# key → (назва в застосунку, що робить, хто може за замовчуванням)
COMMANDS: dict[str, tuple[str, str, list[str]]] = {
    "request": ("Замовити трек", "назва пісні або посилання YouTube", ["all"]),
    "now": ("Що грає", "трек, хто замовив і посилання", ["all"]),
    "queue": ("Черга", "наступні треки", ["all"]),
    "wrong": ("Прибрати свій трек", "останнє власне замовлення", ["all"]),
    "help": ("Підказка", "список доступних команд", ["all"]),
    "skip": ("Пропустити", "увімкнути наступний трек", ["helpers", "mods"]),
    "volume": ("Гучність", "число від 0 до 100", ["helpers", "mods"]),
    "seek": ("Перемотати", "1:30 — на позицію, +30 або -15 — на секунди", ["mods"]),
    "pause": ("Пауза", "поставити на паузу або продовжити", ["mods"]),
    "play": ("Увімкнути музику", "якщо музику вимкнено", ["mods"]),
    "ban": ("Заборонити трек", "назавжди, і одразу пропустити", ["mods"]),
}

LIMIT_KEYS = ("max_per_user", "cooldown", "max_duration", "bypass_queue")
UNLIMITED = {"max_per_user": 0, "cooldown": 0, "max_duration": 0, "bypass_queue": True}

# ліміти ролей за замовчуванням; «Усі» беруться з [rules]
DEFAULT_LIMITS: dict[str, dict] = {
    "subs": {"custom": False, "max_per_user": 3, "cooldown": 15, "max_duration": 600, "bypass_queue": False},
    "vip": {"custom": False, "max_per_user": 3, "cooldown": 15, "max_duration": 600, "bypass_queue": False},
    "helpers": {"custom": False, "max_per_user": 3, "cooldown": 15, "max_duration": 600, "bypass_queue": False},
    "mods": {"custom": True, **UNLIMITED},
}

NAME_RE = re.compile(r"![^\s!@#]{1,24}")

# who_can_request зі старих версій → хто може замовляти
_WHO_TO_ROLES = {"all": ["all"], "subs": ["subs", "vip", "mods"], "vip": ["vip", "mods"], "mods": ["mods"]}


def normalize_name(name: str) -> str:
    n = str(name).strip().lower()
    if n and not n.startswith("!"):
        n = "!" + n
    return n


def normalize_roles(roles) -> list[str]:
    if isinstance(roles, str):
        roles = [roles]
    out = [r for r in dict.fromkeys(str(x).strip().lower() for x in roles or []) if r in ROLE_KEYS]
    return ["all"] if "all" in out else out


def default_permissions(rules: dict | None = None) -> dict[str, list[str]]:
    perms = {k: list(v[2]) for k, v in COMMANDS.items()}
    who = (rules or {}).get("who_can_request")
    if who in _WHO_TO_ROLES:
        perms["request"] = list(_WHO_TO_ROLES[who])
    return perms


def _limit_value(key: str, v):
    if key == "bypass_queue" or key == "custom":
        return bool(v)
    try:
        return max(0, int(v))
    except (TypeError, ValueError):
        return 0


def normalize_limits(raw: dict | None) -> dict:
    out = {}
    for role, base in DEFAULT_LIMITS.items():
        src = (raw or {}).get(role) or {}
        out[role] = {k: _limit_value(k, src.get(k, dv)) for k, dv in base.items()}
    return out


def limits_for_all(rules: dict) -> dict:
    return {
        "max_per_user": _limit_value("max_per_user", rules.get("max_per_user", 0)),
        "cooldown": _limit_value("cooldown", rules.get("cooldown", 0)),
        "max_duration": _limit_value("max_duration", rules.get("max_duration", 0)),
        "bypass_queue": False,
    }


class Access:
    """Перевірки доступу. Читає cfg щоразу, тож зміни з застосунку діють одразу."""

    def __init__(self, cfg):
        self.cfg = cfg

    def roles_of(self, m) -> set[str]:
        roles = {"all"}
        if m.is_sub:
            roles.add("subs")
        if m.is_vip:
            roles.add("vip")
        login = m.login.lower()
        if login in (self.cfg["twitch"].get("helpers") or []):
            roles.add("helpers")
        if m.is_mod or login in (self.cfg["twitch"].get("admins") or []):
            roles.add("mods")
        return roles

    def roles_for(self, key: str) -> list[str]:
        perms = self.cfg.raw.get("permissions") or {}
        if key in perms:
            return perms[key]
        return list(COMMANDS.get(key, ("", "", ["mods"]))[2])

    def can(self, m, key: str) -> bool:
        if m.is_broadcaster:
            return True
        allowed = self.roles_for(key)
        return "all" in allowed or bool(self.roles_of(m) & set(allowed))

    def role_limits(self, role: str) -> dict:
        base = limits_for_all(self.cfg["rules"])
        if role == "all":
            return base
        lim = (self.cfg.raw.get("limits") or {}).get(role) or DEFAULT_LIMITS.get(role, {})
        if not lim.get("custom"):
            return base
        return {k: lim.get(k, base[k]) for k in LIMIT_KEYS}

    def limits(self, m) -> dict:
        """Найщедріші ліміти серед усіх ролей глядача. 0 означає «без обмежень»."""
        if m.is_broadcaster:
            return dict(UNLIMITED)
        best: dict | None = None
        for role in self.roles_of(m):
            lim = self.role_limits(role)
            if best is None:
                best = dict(lim)
                continue
            for k in ("max_per_user", "max_duration"):
                if best[k] and (lim[k] == 0 or lim[k] > best[k]):
                    best[k] = lim[k]
            best["cooldown"] = min(best["cooldown"], lim["cooldown"])
            best["bypass_queue"] = best["bypass_queue"] or lim["bypass_queue"]
        return best or limits_for_all(self.cfg["rules"])
