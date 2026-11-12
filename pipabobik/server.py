"""Локальний веб-сервер: віджет для OBS, панель керування, авторизація Twitch."""
from __future__ import annotations

import asyncio
import json
import logging
import re
import secrets
from pathlib import Path

from aiohttp import web

log = logging.getLogger("pipabobik.server")

NICK_RE = re.compile(r"[a-z0-9_]{1,25}")  # логін Twitch


def _num(v) -> int:
    """Невід'ємне ціле з поля застосунку; сміття вважаємо нулем."""
    try:
        return max(0, int(float(v or 0)))
    except (TypeError, ValueError):
        return 0

WEB_DIR = Path(__file__).resolve().parent.parent / "web"


class Server:
    def __init__(self, bot, port: int, data_dir: Path):
        self.bot = bot
        self.port = port
        self.key = self._load_key(data_dir)
        self._clients: set[asyncio.Queue] = set()
        self.app = web.Application()
        r = self.app.router
        r.add_get("/", self.index)
        r.add_get("/widget", self.page("widget.html"))
        r.add_get("/panel", self.page("panel.html"))
        r.add_get("/app", self.page("app.html"))
        r.add_get("/favicon.ico", self.favicon)
        r.add_post("/api/search", self.api_search)
        r.add_post("/api/add", self.api_add)
        r.add_get("/api/settings", self.api_settings_get)
        r.add_post("/api/settings", self.api_settings_set)
        r.add_get("/api/access", self.api_access_get)
        r.add_post("/api/access", self.api_access_set)
        r.add_get("/api/state", self.api_state)
        r.add_get("/api/events", self.api_events)
        r.add_post("/api/control", self.api_control)
        r.add_get("/auth", self.auth_start)
        r.add_get("/auth/callback", self.auth_callback)
        r.add_static("/static", WEB_DIR / "static")
        self.runner: web.AppRunner | None = None

    @staticmethod
    def _load_key(data_dir: Path) -> str:
        p = data_dir / "panel.key"
        if p.exists():
            return p.read_text().strip()
        data_dir.mkdir(parents=True, exist_ok=True)
        k = secrets.token_urlsafe(12)
        p.write_text(k)
        return k

    async def start(self) -> None:
        self.runner = web.AppRunner(self.app, access_log=None)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", self.port)
        await site.start()  # OSError, якщо порт зайнятий (бот вже запущено)

    async def stop(self) -> None:
        if self.runner:
            await self.runner.cleanup()

    # ── стан ──
    def payload(self) -> dict:
        st = self.bot.jukebox.state()
        st["health"] = self.bot.health()
        return st

    def push(self) -> None:
        if not self._clients:
            return
        data = json.dumps(self.payload(), ensure_ascii=False)
        for q in list(self._clients):
            if q.qsize() < 20:
                q.put_nowait(data)

    async def api_state(self, request: web.Request) -> web.Response:
        return web.json_response(self.payload(), headers={"Access-Control-Allow-Origin": "*"})

    async def api_events(self, request: web.Request) -> web.StreamResponse:
        resp = web.StreamResponse(
            headers={
                "Content-Type": "text/event-stream",
                "Cache-Control": "no-cache",
                "Access-Control-Allow-Origin": "*",
            }
        )
        await resp.prepare(request)
        q: asyncio.Queue = asyncio.Queue()
        self._clients.add(q)
        try:
            await resp.write(f"data: {json.dumps(self.payload(), ensure_ascii=False)}\n\n".encode())
            while True:
                try:
                    data = await asyncio.wait_for(q.get(), timeout=15)
                    await resp.write(f"data: {data}\n\n".encode())
                except asyncio.TimeoutError:
                    await resp.write(b": ping\n\n")
        except (ConnectionResetError, asyncio.CancelledError):
            pass
        finally:
            self._clients.discard(q)
        return resp

    # ── сторінки ──
    def page(self, name: str):
        async def handler(request: web.Request) -> web.Response:
            return web.FileResponse(WEB_DIR / name, headers={"Cache-Control": "no-cache"})

        return handler

    async def index(self, request: web.Request) -> web.Response:
        b = self.bot
        authed = b.auth.authorized.is_set()
        base = f"http://localhost:{self.port}"
        rows = [
            ("Twitch", f"✅ увійшов як <b>{b.auth.login}</b>" if authed else '❌ <a href="/auth">увійти в Twitch</a>'),
            ("Канал", f"#{b.cfg.channel}" if b.cfg.channel else "❌ не вказано в config.toml"),
            ("Чат", "✅ підключено" if b.chat.connected.is_set() else "⏳ підключення…"),
            ("mpv", "✅ знайдено" if b.player.available else "❌ не знайдено — встановіть mpv"),
            ("Віджет", f'<code>{base}/widget</code>'),
            ("Панель (док OBS)", f'<code>{base}/panel?key={self.key}</code>'),
        ]
        html = (
            "<!doctype html><meta charset=utf-8><title>pipabobik</title>"
            "<style>body{font:15px system-ui;background:#1a0f17;color:#fbe7f2;padding:32px}"
            "h1{color:#ff5fa8}td{padding:8px 14px;border-bottom:1px solid #3a2232}"
            "a{color:#ff8cc6}code{background:#2c1726;padding:3px 6px;border-radius:6px}</style>"
            "<h1>🎀 pipabobik</h1><table>"
            + "".join(f"<tr><td>{k}</td><td>{v}</td></tr>" for k, v in rows)
            + "</table>"
        )
        return web.Response(text=html, content_type="text/html")

    # ── керування з панелі ──
    def _authorized(self, request: web.Request) -> bool:
        return request.headers.get("X-Pipabobik-Key") == self.key

    async def favicon(self, request: web.Request) -> web.Response:
        return web.FileResponse(WEB_DIR / "static" / "pipabobik.ico")

    async def api_control(self, request: web.Request) -> web.Response:
        if not self._authorized(request):
            return web.json_response({"error": "bad key"}, status=403)
        d = await request.json()
        jb = self.bot.jukebox
        action = d.get("action")
        if action == "skip":
            await jb.skip()
        elif action == "pause":
            await jb.set_pause(not jb.player.paused)
        elif action == "volume":
            await jb.set_volume(int(d.get("value", jb.player.volume)))
        elif action == "seek":
            if not jb.now:
                return web.json_response({"error": "Зараз нічого не грає"}, status=409)
            try:
                pos = float(d["position"]) if "position" in d else jb.player.position + float(d.get("delta", 0))
            except (TypeError, ValueError):
                return web.json_response({"error": "Невірна позиція"}, status=400)
            if await jb.seek(pos) is None:
                return web.json_response({"error": "Не вдалося перемотати"}, status=409)
        elif action == "remove":
            jb.remove_at(int(d.get("index", -1)))
        elif action == "up":
            jb.move_up(int(d.get("index", -1)))
        elif action == "move":
            jb.move(int(d.get("index", -1)), int(d.get("to", -1)))
        elif action == "next":  # поставити трек із черги першим
            jb.move(int(d.get("index", -1)), 0)
        elif action == "clear":
            jb.clear()
        elif action == "unban":
            jb.unban(str(d.get("video_id", "")))
        elif action == "like":
            from .music import LoginRequired

            vid = str(d.get("video_id") or (jb.now.video_id if jb.now else ""))
            if not vid:
                return web.json_response({"error": "Зараз нічого не грає"}, status=409)
            want = bool(d["like"]) if "like" in d else vid not in jb.liked_ids
            try:
                await jb.like(vid, want)
            except LoginRequired:
                return web.json_response({"error": "Щоб лайкати, увійдіть у music.youtube.com у Firefox і перезапустіть бота"}, status=409)
            except Exception as e:  # noqa: BLE001
                log.warning("Лайк не вдався: %s", e)
                return web.json_response({"error": "YouTube Music не прийняв лайк — спробуйте ще раз"}, status=502)
        elif action == "ban":
            await jb.ban_current()
        elif action == "on":
            jb.activate()
        elif action == "off":
            await jb.deactivate()
        elif action == "shutdown":
            asyncio.get_running_loop().call_later(0.3, self.bot.shutdown.set)
            return web.json_response({"ok": True})
        else:
            return web.json_response({"error": "unknown action"}, status=400)
        return web.json_response(self.payload())

    # ── пошук і додавання з панелі ──
    async def api_search(self, request: web.Request) -> web.Response:
        if not self._authorized(request):
            return web.json_response({"error": "bad key"}, status=403)
        q = str((await request.json()).get("q", "")).strip()[:200]
        if not q:
            return web.json_response({"results": []})
        from .music import extract_video_id

        vid = extract_video_id(q)
        if vid:
            t = await self.bot.music.by_id(vid)
            results = [t] if t else []
        else:
            results = await self.bot.music.search(q, only_songs=bool(self.bot.cfg["rules"].get("only_songs", True)))
        return web.json_response({"results": [t.to_dict() for t in results[:8]]})

    async def api_add(self, request: web.Request) -> web.Response:
        if not self._authorized(request):
            return web.json_response({"error": "bad key"}, status=403)
        from .music import Track

        d = await request.json()
        try:
            track = Track.from_dict(d.get("track") or {})
        except TypeError:
            return web.json_response({"error": "bad track"}, status=400)
        jb = self.bot.jukebox
        if track.video_id in jb.banned_ids:
            return web.json_response({"error": f"{track.label} у бан-листі. Розбаньте його у вкладці «Історія», якщо хочете зіграти."}, status=409)
        reason = await jb.filter_reason(track)
        if reason:
            jb.ban_track(track, reason)
            return web.json_response({"error": f"{track.label} — {reason}. Трек додано в бан-лист."}, status=409)
        mode = d.get("mode", "end")  # end | next | now
        who = self.bot.auth.login or self.bot.cfg.channel or "стрімер"
        jb.add(track, "panel", who, front=mode in ("next", "now"))
        if mode == "now":
            if not jb.active:
                jb.activate()
            elif jb.now:
                await jb.skip()
        return web.json_response(self.payload())

    # ── налаштування ──
    SETTINGS_SCHEMA = {
        "rules": {
            "max_duration": int, "max_per_user": int, "cooldown": int,
            "max_queue": int, "only_songs": bool, "block_explicit": bool, "interrupt_fallback": bool,
            "banned_words": list, "block_russian": bool, "russian_artists": list,
        },
        "chat": {"announce_tracks": bool, "greet_on_stream_start": bool},
        "player": {"normalize": bool},
        "google": {"fallback_playlist": str, "fallback_shuffle": bool},
        "twitch": {"helpers": list},
    }

    async def api_settings_get(self, request: web.Request) -> web.Response:
        if not self._authorized(request):
            return web.json_response({"error": "bad key"}, status=403)
        cfg = self.bot.cfg
        out = {sec: {k: cfg[sec].get(k) for k in keys} for sec, keys in self.SETTINGS_SCHEMA.items()}
        out["commands"] = {k: v[0] for k, v in cfg["commands"].items() if v}  # лише для підказок
        return web.json_response(out)

    async def api_settings_set(self, request: web.Request) -> web.Response:
        if not self._authorized(request):
            return web.json_response({"error": "bad key"}, status=403)
        from .config import nick, set_values

        d = await request.json()
        cfg = self.bot.cfg
        updates: dict[str, dict] = {}
        for sec, keys in self.SETTINGS_SCHEMA.items():
            for k, typ in keys.items():
                if k not in (d.get(sec) or {}):
                    continue
                v = d[sec][k]
                try:
                    if typ is bool:
                        v = bool(v)
                    elif typ is int:
                        v = max(0, int(v))
                    elif typ is list:
                        v = [str(x).strip() for x in (v if isinstance(v, list) else str(v).split(",")) if str(x).strip()]
                        if k == "banned_words":
                            v = [x.lower() for x in v]
                        elif k == "helpers":
                            v = [n for n in dict.fromkeys(nick(x) for x in v) if NICK_RE.fullmatch(n)]
                    else:
                        v = str(v).strip()
                except (TypeError, ValueError):
                    return web.json_response({"error": f"{sec}.{k}: невірне значення"}, status=400)
                cfg[sec][k] = v  # застосовується одразу — ті самі словники використовують черга й команди
                updates.setdefault(sec, {})[k] = v
        if updates:
            set_values(cfg.path, updates)
            self.bot.apply_settings(updates)
        return web.json_response({"ok": True})

    # ── команди, ролі, ліміти ──
    def _access_payload(self) -> dict:
        from .access import COMMANDS, ROLES, Access

        cfg = self.bot.cfg
        acc = Access(cfg)
        limits = {"all": acc.role_limits("all")}
        for role, lim in cfg["limits"].items():
            limits[role] = dict(lim)
        return {
            "roles": [{"key": k, "label": v} for k, v in ROLES],
            "commands": [
                {"key": k, "label": label, "desc": desc, "names": list(cfg["commands"].get(k, [])), "roles": acc.roles_for(k)}
                for k, (label, desc, _) in COMMANDS.items()
            ],
            "limits": limits,
            "helpers": list(cfg["twitch"].get("helpers") or []),
            "max_queue": int(cfg["rules"].get("max_queue", 30)),
        }

    async def api_access_get(self, request: web.Request) -> web.Response:
        if not self._authorized(request):
            return web.json_response({"error": "bad key"}, status=403)
        return web.json_response(self._access_payload())

    async def api_access_set(self, request: web.Request) -> web.Response:
        if not self._authorized(request):
            return web.json_response({"error": "bad key"}, status=403)
        from .access import COMMANDS, DEFAULT_LIMITS, LIMIT_KEYS, NAME_RE, normalize_name, normalize_roles
        from .config import nick, set_values

        d = await request.json()
        cfg = self.bot.cfg
        updates: dict[str, dict] = {}
        err = lambda text: web.json_response({"error": text}, status=400)  # noqa: E731

        # назви команд і хто може
        cmds = d.get("commands") or {}
        new_names = {k: list(v) for k, v in cfg["commands"].items()}
        new_perms: dict[str, list[str]] = {}
        for key, upd in cmds.items():
            if key not in COMMANDS or not isinstance(upd, dict):
                continue
            label = COMMANDS[key][0]
            if "names" in upd:
                names = [n for n in dict.fromkeys(normalize_name(x) for x in upd["names"] or []) if n]
                if not names:
                    return err(f"«{label}»: потрібна хоча б одна назва")
                bad = [n for n in names if not NAME_RE.fullmatch(n)]
                if bad:
                    return err(f"«{bad[0]}» не підходить: одне слово з ! на початку, до 25 символів, без пробілів")
                new_names[key] = names
            if "roles" in upd:
                new_perms[key] = normalize_roles(upd["roles"])
        owner: dict[str, str] = {}
        for key, names in new_names.items():
            for n in names:
                if n in owner and owner[n] != key:
                    a = COMMANDS.get(owner[n], (owner[n],))[0]
                    return err(f"{n} уже зайнято командою «{a}»")
                owner[n] = key

        # ліміти ролей
        lim_in = d.get("limits") or {}
        rules_upd: dict = {}
        role_upd: dict[str, dict] = {}
        for role, vals in lim_in.items():
            if not isinstance(vals, dict):
                continue
            if role == "all":
                for k in ("max_per_user", "cooldown", "max_duration"):
                    if k in vals:
                        rules_upd[k] = _num(vals[k])
            elif role in DEFAULT_LIMITS:
                cur = dict(cfg["limits"][role])
                for k in ("custom", *LIMIT_KEYS):
                    if k in vals:
                        cur[k] = bool(vals[k]) if k in ("custom", "bypass_queue") else _num(vals[k])
                role_upd[role] = cur
        if "max_queue" in d:
            rules_upd["max_queue"] = max(1, _num(d["max_queue"]))

        helpers = None
        if "helpers" in d:
            helpers = [n for n in dict.fromkeys(nick(x) for x in d["helpers"] or []) if NICK_RE.fullmatch(n)]

        # усе перевірено — застосовуємо одразу і зберігаємо в config.toml
        changed_names = {k: v for k, v in new_names.items() if v != cfg["commands"].get(k)}
        if changed_names:
            cfg["commands"].update(changed_names)
            updates["commands"] = changed_names
        if new_perms:
            cfg["permissions"].update(new_perms)
            updates["permissions"] = new_perms
        if rules_upd:
            cfg["rules"].update(rules_upd)
            updates["rules"] = rules_upd
        for role, cur in role_upd.items():
            cfg["limits"][role] = cur
            updates[f"limits.{role}"] = cur
        if helpers is not None:
            cfg["twitch"]["helpers"] = helpers
            updates["twitch"] = {"helpers": helpers}
        if updates:
            set_values(cfg.path, updates)
            commands = getattr(self.bot, "commands", None)  # з'являються, коли бот підключився до чату
            if changed_names and commands:
                commands.rebuild()
            log.info("Доступ оновлено з панелі: %s", ", ".join(updates))
        return web.json_response(self._access_payload())

    # ── авторизація Twitch ──
    async def auth_start(self, request: web.Request) -> web.Response:
        if not self.bot.auth.client_id:
            return web.Response(text="Вкажіть client_id у config.toml", status=400)
        raise web.HTTPFound(self.bot.auth.authorize_url())

    async def auth_callback(self, request: web.Request) -> web.Response:
        code, state = request.query.get("code"), request.query.get("state", "")
        if not code:
            err = request.query.get("error_description") or "авторизацію скасовано"
            return web.Response(text=f"❌ {err}", content_type="text/plain", charset="utf-8")
        try:
            await self.bot.auth.exchange_code(self.bot.session, code, state)
        except Exception as e:  # noqa: BLE001
            log.exception("auth")
            return web.Response(text=f"❌ {e}", content_type="text/plain", charset="utf-8")
        html = (
            "<!doctype html><meta charset=utf-8><body style='font:18px system-ui;background:#1a0f17;"
            "color:#fbe7f2;display:grid;place-items:center;height:90vh'>"
            f"<div>✅ Готово! pipabobik увійшов як <b>{self.bot.auth.login}</b>. Вкладку можна закрити.</div>"
        )
        return web.Response(text=html, content_type="text/html")
