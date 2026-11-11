#!/usr/bin/env python3
"""pipabobik — музичний бот для Twitch на YouTube Music.

  python bot.py            запуск
  python bot.py --setup    майстер налаштування (перший запуск)
  python bot.py --check    діагностика: Twitch, mpv, Deno, логін Google, пошук, звук
  python bot.py --obs      режим для OBS-скрипта (музика грає лише під час трансляції)
"""
from __future__ import annotations

import argparse
import asyncio
import shutil
import sys
import webbrowser

if sys.platform == "win32":  # українські літери та емодзі в консолі Windows
    for stream in (sys.stdout, sys.stderr):
        if stream is not None:
            try:
                stream.reconfigure(encoding="utf-8")
            except (AttributeError, ValueError):
                pass

from pipabobik.config import CONFIG_PATH, load_config, set_values  # noqa: E402

PINK = "\033[38;5;205m"
RESET = "\033[0m"


def ask(prompt: str, default: str = "") -> str:
    hint = f" [{default}]" if default else ""
    val = input(f"{PINK}?{RESET} {prompt}{hint}: ").strip()
    return val or default


def setup_wizard() -> None:
    cfg = load_config()
    tw = cfg["twitch"]
    print(f"\n{PINK}🎀 Налаштування pipabobik{RESET}\n")
    print("1) Канал — ваш нік на Twitch (де стрімите).")
    channel = ask("Нік каналу", cfg.channel)

    print(
        "\n2) Застосунок Twitch. Відкрийте https://dev.twitch.tv/console/apps → «Register Your Application»:\n"
        "   • Name: будь-яка, напр. pipabobik-<ваш нік>\n"
        f"   • OAuth Redirect URLs: http://localhost:{cfg.port}/auth/callback\n"
        "   • Category: Chat Bot · Client Type: Confidential\n"
        "   Потім «Manage» → скопіюйте Client ID і натисніть «New Secret»."
    )
    if ask("Відкрити сторінку в браузері? (т/н)", "т").lower().startswith(("т", "y")):
        webbrowser.open("https://dev.twitch.tv/console/apps/create")
    client_id = ask("Client ID", tw["client_id"])
    client_secret = ask("Client Secret", tw["client_secret"])

    print(
        "\n3) Логін Google для YouTube Music.\n"
        "   Найпростіше — увійти в music.youtube.com у Firefox (Chrome не віддає логін стороннім програмам).\n"
        "   Варіанти: firefox / файл cookies.txt / порожньо (без логіну)."
    )
    g = cfg["google"]
    browser = ask("Браузер з логіном (firefox або Enter, щоб пропустити)", g["cookies_from_browser"])
    cookies_file = ""
    if browser.lower() in ("файл", "file", "cookies.txt"):
        cookies_file, browser = ask("Шлях до cookies.txt"), ""
    print("\n4) Що грати, коли черга порожня: LM = ваші вподобані в YT Music, або посилання на плейлист, або «-» для тиші.")
    fallback = ask("Фоновий плейлист", g["fallback_playlist"] or "LM")
    if fallback == "-":
        fallback = ""

    set_values(
        CONFIG_PATH,
        {
            "twitch": {"channel": channel.lower().lstrip("#@"), "client_id": client_id, "client_secret": client_secret},
            "google": {"cookies_from_browser": browser.lower(), "cookies_file": cookies_file, "fallback_playlist": fallback},
        },
    )
    print(f"\n✅ Збережено в {CONFIG_PATH.name}. Решта налаштувань (ліміти, команди) — там же.\n")
    print(
        "Зараз запуститься бот і відкриється сторінка входу Twitch.\n"
        "⚠ Входьте тим акаунтом, від імені якого писатиме бот (рекомендуємо окремий акаунт «pipabobik»,\n"
        "  і видайте йому модерку на каналі: /mod pipabobik). Найзручніше — у приватному вікні.\n"
    )
    input("Enter — продовжити…")


async def run_check() -> int:
    from pipabobik.config import load_config
    from pipabobik.music import MusicSource
    from pipabobik.player import Player

    cfg = load_config()
    ok = True

    def line(good: bool, text: str) -> None:
        nonlocal ok
        ok &= good
        print(("  ✅ " if good else "  ❌ ") + text)

    print(f"\n{PINK}🎀 Діагностика pipabobik{RESET}")
    line(CONFIG_PATH.exists(), "config.toml існує" if CONFIG_PATH.exists() else "немає config.toml — запустіть --setup")
    line(bool(cfg.channel), f"канал: {cfg.channel or '—'}")
    line(bool(cfg['twitch']['client_id'] and cfg['twitch']['client_secret']), "client_id / client_secret")
    p = Player(cfg["player"]["mpv_path"])
    line(p.available, f"mpv: {p.mpv or 'не знайдено → winget install -e --id shinchiro.mpv, потім перезапустіть вікно'}")
    if not p.available:
        print("     або знайдіть mpv.exe і впишіть у config.toml в одинарних лапках: mpv_path = 'C:\\шлях\\mpv.exe'")
    deno = shutil.which("deno")
    line(bool(deno), f"Deno (потрібен yt-dlp для YouTube): {deno or 'не знайдено → winget install -e --id DenoLand.Deno'}")

    g = cfg["google"]
    music = MusicSource(g["cookies_from_browser"], g["cookies_file"])
    try:
        import yt_dlp

        with yt_dlp.YoutubeDL(music.ydl_opts()) as ydl:
            yt = [c for c in ydl.cookiejar if "youtube.com" in c.domain]
        logged = any(c.name in ("SAPISID", "__Secure-3PAPISID") for c in yt)
        line(logged or not (g["cookies_from_browser"] or g["cookies_file"]),
             "логін Google знайдено" if logged else "логін Google не знайдено (увійдіть у music.youtube.com у Firefox)")
    except Exception as e:  # noqa: BLE001
        line(False, f"cookies: {e}")

    tracks = await music.search("Океан Ельзи Обійми")
    line(bool(tracks), f"пошук YT Music: {tracks[0].label if tracks else 'не працює'}")
    if tracks:
        res = await music.resolve_stream(tracks[0])
        line(bool(res), "отримання аудіо через yt-dlp" + ("" if res else " — не вдалося (оновіть: pip install -U yt-dlp[default])"))
        if res and p.available:
            print("  🔊 Граю 5 секунд…")
            task = asyncio.create_task(p.play(*res))
            await asyncio.sleep(5)
            await p.stop()
            await task
    print("\n" + ("Все гаразд! 🎉" if ok else "Є проблеми — див. ❌ вище та розділ «Якщо щось не працює» в README."))
    return 0 if ok else 1


def main() -> int:
    ap = argparse.ArgumentParser(description="pipabobik — музичний бот для Twitch")
    ap.add_argument("--setup", action="store_true", help="майстер налаштування")
    ap.add_argument("--check", action="store_true", help="діагностика")
    ap.add_argument("--obs", action="store_true", help="запуск з OBS: музика грає лише під час трансляції")
    ap.add_argument("--live", action="store_true", help="разом з --obs: трансляція вже йде")
    ap.add_argument("--paused", action="store_true", help="не вмикати музику одразу (вмикається з панелі)")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    if args.setup:
        setup_wizard()
    if args.check:
        return asyncio.run(run_check())

    from pipabobik.app import Bot, setup_logging

    setup_logging(args.verbose)
    bot = Bot(load_config(), obs_mode=args.obs, live=args.live, paused=args.paused)
    try:
        return asyncio.run(bot.run())
    except KeyboardInterrupt:
        bot.jukebox.save()
        return 0


if __name__ == "__main__":
    sys.exit(main())
