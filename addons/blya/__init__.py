"""Додаток «!бля»: виправляє повідомлення, набране не в тій розкладці.

Хтось написав «ghbdsn dcsv», потім «!бля» — бот відповідає на те повідомлення:
«Pipa_Fan хотів гавкнуть: привіт всім».

Що виправляється:
  * «!бля» — останнє власне повідомлення (не старіше 15 хв);
  * «!бля» відповіддю (Reply) на чуже повідомлення — саме його;
  * «!бля @нік» — останнє повідомлення цієї людини;
  * «!бля ghbdsn» — текст після команди.
Нік автора, @згадки, емоути й посилання не перекладаються.

Підключення — див. README.md поруч. Основний код бота цей додаток не змінює.
"""
from __future__ import annotations

import logging
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Awaitable, Callable

from .layout import fix_text

log = logging.getLogger("pipabobik.addon.blya")

# «!,kz» — та сама «!бля», набрана в англійській розкладці
NAMES = ("!бля", "!,kz")
TEMPLATE = "{name} хотів гавкнуть: {text}"
MAX_AGE = 15 * 60  # секунд: старіші повідомлення вже не виправляємо
USER_COOLDOWN = 10
GLOBAL_COOLDOWN = 3
TWITCH_LIMIT = 500

Say = Callable[[str, "str | None"], Awaitable[None]]

_KEEP = ""  # позначка для фрагментів, які не можна чіпати (емоути, згадки)


@dataclass
class Said:
    message_id: str
    login: str
    name: str
    parts: list[tuple[str, str]]  # (тип фрагмента, текст): text | mention | emote | cheermote
    at: float

    @property
    def plain(self) -> str:
        return "".join(t for _, t in self.parts).strip()


def _parts_from_event(ev: dict) -> list[tuple[str, str]]:
    """Фрагменти повідомлення без «@нік» на початку відповіді (його Twitch додає сам)."""
    msg = ev.get("message") or {}
    frags = msg.get("fragments") or [{"type": "text", "text": msg.get("text", "")}]
    parts = [(f.get("type") or "text", f.get("text") or "") for f in frags]
    login = ((ev.get("reply") or {}).get("parent_user_login") or "").lower()
    if login and parts:
        kind, text = parts[0]
        stripped = text.lstrip()
        prefix = "@" + login
        if stripped.lower().startswith(prefix):
            rest = stripped[len(prefix):]
            parts = ([("text", rest)] if rest.strip() else []) + parts[1:]
            if parts and parts[0][0] == "text":
                parts[0] = ("text", parts[0][1].lstrip())
    return parts


def fix_parts(parts: list[tuple[str, str]]) -> tuple[str, bool]:
    """Виправляє лише текстові фрагменти; емоути, згадки й чірмоути лишаються як були."""
    kept: list[str] = []
    buf = []
    for kind, text in parts:
        if kind == "text":
            buf.append(text)
        else:
            buf.append(f"{_KEEP}{len(kept)}{_KEEP}")
            kept.append(text)
    fixed, changed = fix_text("".join(buf))
    for i, text in enumerate(kept):
        fixed = fixed.replace(f"{_KEEP}{i}{_KEEP}", text)
    return fixed.strip(), changed


class BlyaAddon:
    def __init__(self, say: Say, bot_login: Callable[[], str | None] = lambda: None):
        self.say = say
        self.bot_login = bot_login  # логін бота стає відомий лише після входу в Twitch
        self.last: dict[str, Said] = {}  # login → останнє звичайне повідомлення
        self.by_id: OrderedDict[str, Said] = OrderedDict()  # для відповідей на конкретне повідомлення
        self._used: dict[str, float] = {}
        self._last_any = 0.0

    # ── облік повідомлень ──
    def observe(self, cm, ev: dict | None) -> None:
        if not cm.text or cm.text.startswith("!"):
            return
        parts = _parts_from_event(ev) if ev else [("text", cm.text)]
        said = Said(cm.message_id, cm.login.lower(), cm.display_name or cm.login, parts, time.time())
        self.last[said.login] = said
        self.by_id[said.message_id] = said
        while len(self.by_id) > 500:
            self.by_id.popitem(last=False)

    @staticmethod
    def is_command(cm) -> bool:
        return bool(cm.text) and cm.text.split(maxsplit=1)[0].lower() in NAMES

    # ── сама команда ──
    def _target(self, cm, ev: dict | None, arg: str) -> Said | None:
        reply = (ev or {}).get("reply") or {}
        if reply.get("parent_message_id"):
            known = self.by_id.get(reply["parent_message_id"])
            if known:
                return known
            body = reply.get("parent_message_body") or ""
            return Said(reply["parent_message_id"], (reply.get("parent_user_login") or "").lower(),
                        reply.get("parent_user_name") or reply.get("parent_user_login") or "", [("text", body)], time.time())
        if arg.startswith("@"):
            said = self.last.get(arg[1:].split()[0].lower().rstrip(",.:"))
        elif arg:
            # «!бля ghbdsn» — виправляємо сам текст після команди
            return Said(cm.message_id, cm.login.lower(), cm.display_name or cm.login, [("text", arg)], time.time())
        else:
            said = self.last.get(cm.login.lower())
        if said and time.time() - said.at > MAX_AGE:
            return None
        return said

    def _cooling(self, login: str) -> bool:
        now = time.time()
        if now - self._last_any < GLOBAL_COOLDOWN or now - self._used.get(login, 0) < USER_COOLDOWN:
            return True
        self._used[login] = self._last_any = now
        return False

    async def handle(self, cm, ev: dict | None = None) -> None:
        if self._cooling(cm.login.lower()):
            return
        parts = cm.text.split(maxsplit=1)
        arg = parts[1].strip() if len(parts) > 1 else ""
        target = self._target(cm, ev, arg)
        if not target or not target.plain:
            return await self.say("не бачу, що виправляти 🐶 Напиши !бля одразу після свого повідомлення або відповіддю на потрібне", cm.message_id)
        if target.login and target.login == (self.bot_login() or "").lower():
            return  # власні повідомлення бота не «виправляємо»
        fixed, changed = fix_parts(target.parts)
        if not changed:
            return await self.say(f"у {target.name} і так усе зрозуміло 🐶", cm.message_id)
        text = TEMPLATE.format(name=target.name, text=fixed)
        if len(text) > TWITCH_LIMIT:
            text = text[: TWITCH_LIMIT - 1] + "…"
        log.info("!бля від %s: %r → %r", cm.login, target.plain, fixed)
        await self.say(text, target.message_id or cm.message_id)


def _keep_raw_events() -> None:
    """ChatMessage не зберігає фрагменти й дані відповіді — додаємо сирий event як .raw."""
    from pipabobik.twitch import ChatMessage

    if getattr(ChatMessage, "_blya_raw", False):
        return
    original = ChatMessage.from_event.__func__

    def from_event(cls, ev):
        cm = original(cls, ev)
        cm.raw = ev
        return cm

    ChatMessage.from_event = classmethod(from_event)
    ChatMessage._blya_raw = True


def install(bot) -> BlyaAddon:
    """Вмикає «!бля». Викликати після того, як бот призначив обробник чату."""
    _keep_raw_events()
    addon = BlyaAddon(bot.say, lambda: getattr(bot.auth, "login", None))
    original = bot.chat.handler

    async def handler(cm):
        ev = getattr(cm, "raw", None)
        if BlyaAddon.is_command(cm):
            return await addon.handle(cm, ev)
        addon.observe(cm, ev)
        if original:
            await original(cm)

    bot.chat.handler = handler
    log.info("Додаток «!бля» увімкнено")
    return addon
