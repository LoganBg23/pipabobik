"""pipabobik — панель керування окремим вікном.

Запуск: подвійний клік по ярлику «pipabobik» на робочому столі
(або OBS відкриває її сам, якщо в скрипті ввімкнено «Відкривати панель»).

Якщо бот ще не працює — панель запускає його:
  • OBS відкрито  → у режимі OBS (музика з початком трансляції, закриється разом з OBS);
  • OBS закрито   → на паузі (музику вмикаєте перемикачем у панелі).
Вікно — це Microsoft Edge у режимі застосунку: без вкладок і адресного рядка,
окремий значок на панелі задач. Edge є в кожній Windows 10/11.
"""
from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import time
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from pipabobik.config import DATA_DIR, load_config  # noqa: E402

IS_WIN = sys.platform == "win32"
NO_WINDOW = 0x08000000 if IS_WIN else 0  # CREATE_NO_WINDOW
DETACHED = 0x00000008 if IS_WIN else 0  # DETACHED_PROCESS


def bot_running(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.4):
            return True
    except OSError:
        return False


def wait_bot(port: int, seconds: float) -> bool:
    end = time.time() + seconds
    while time.time() < end:
        if bot_running(port):
            return True
        time.sleep(0.4)
    return False


def obs_running() -> bool:
    try:
        import psutil
    except ImportError:
        return False
    names = {"obs64.exe", "obs32.exe", "obs.exe", "obs"}
    return any((p.info.get("name") or "").lower() in names for p in psutil.process_iter(["name"]))


def start_bot() -> None:
    py = Path(sys.executable)
    if IS_WIN and py.name.lower() == "python.exe" and (py.parent / "pythonw.exe").exists():
        py = py.parent / "pythonw.exe"
    args = [str(py), str(ROOT / "bot.py"), "--obs" if obs_running() else "--paused"]
    subprocess.Popen(args, cwd=ROOT, creationflags=NO_WINDOW | DETACHED, close_fds=True,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def find_browser() -> str | None:
    if not IS_WIN:
        for name in ("microsoft-edge", "google-chrome", "chromium", "chromium-browser"):
            if shutil.which(name):
                return shutil.which(name)
        mac = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
        return str(mac) if mac.exists() else None
    cands = []
    for env in ("ProgramFiles(x86)", "ProgramFiles", "LOCALAPPDATA"):
        base = os.environ.get(env)
        if base:
            cands += [Path(base) / "Microsoft/Edge/Application/msedge.exe",
                      Path(base) / "Google/Chrome/Application/chrome.exe"]
    for c in cands:
        if c.exists():
            return str(c)
    return shutil.which("msedge") or shutil.which("chrome")


def open_window(url: str) -> None:
    browser = find_browser()
    if not browser:
        webbrowser.open(url)
        return
    profile = DATA_DIR / "panel-window"  # окремий легкий профіль: пам'ятає розмір вікна, не чіпає ваш браузер
    args = [
        browser,
        f"--app={url}",
        f"--user-data-dir={profile}",
        "--window-size=480,900",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-extensions",
        "--disable-sync",
        "--disable-background-networking",
        "--disable-features=Translate,msEdgeSidebarV2,msUndersideButton",
    ]
    subprocess.Popen(args, creationflags=DETACHED, close_fds=True,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def main() -> None:
    cfg = load_config()
    port = cfg.port
    # OBS-скрипт міг запустити бота щойно — даємо йому кілька секунд
    if not wait_bot(port, 6):
        start_bot()
        wait_bot(port, 20)
    key_file = DATA_DIR / "panel.key"
    for _ in range(20):
        if key_file.exists():
            break
        time.sleep(0.25)
    key = key_file.read_text().strip() if key_file.exists() else ""
    open_window(f"http://localhost:{port}/app?key={key}")


if __name__ == "__main__":
    main()
