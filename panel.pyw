"""pipabobik — застосунок-панель керування.

Відкривається ярликом «pipabobik» (робочий стіл / меню Пуск / закріплений на панелі задач)
або разом з OBS (галочка в скрипті pipabobik.lua).

Поведінка:
  • має власний значок на панелі задач — його можна закріпити;
  • якщо бот ще не працює — запускає його (OBS відкрито → режим OBS, інакше — на паузі);
  • закрили вікно → pipabobik вимикається повністю: музика зупиняється, черга зберігається;
  • закрили OBS → бот зупиняє музику й закривається, вікно панелі закривається теж;
  • повторний запуск не відкриває друге вікно, а показує вже відкрите.

Вікно — вбудований WebView2 (pywebview). Якщо його немає — Microsoft Edge у режимі застосунку.
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from pipabobik.config import DATA_DIR, load_config  # noqa: E402
from pipabobik.winproc import CREATE_BREAKAWAY_FROM_JOB, kill_children_with_me  # noqa: E402

IS_WIN = sys.platform == "win32"
APP_ID = "LoganBg23.pipabobik.panel"  # той самий ID записано в ярлик — так Windows групує вікно з ним
TITLE = "pipabobik"
ICON = ROOT / "web" / "static" / "pipabobik.ico"
NO_WINDOW = 0x08000000 if IS_WIN else 0
DETACHED = 0x00000008 if IS_WIN else 0

LOG = DATA_DIR / "panel.log"


def log(msg: str) -> None:
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(time.strftime("%H:%M:%S ") + msg + "\n")
    except OSError:
        pass


# ── бот ──
def bot_running(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.4):
            return True
    except OSError:
        return False


def wait_until(cond, seconds: float, step: float = 0.4) -> bool:
    end = time.time() + seconds
    while time.time() < end:
        if cond():
            return True
        time.sleep(step)
    return False


def obs_running() -> bool:
    try:
        import psutil
    except ImportError:
        return False
    names = {"obs64.exe", "obs32.exe", "obs.exe", "obs"}
    for p in psutil.process_iter(["name"]):
        if (p.info.get("name") or "").lower() in names:
            return True
    return False


def start_bot() -> None:
    py = Path(sys.executable)
    if IS_WIN and py.name.lower() == "python.exe" and (py.parent / "pythonw.exe").exists():
        py = py.parent / "pythonw.exe"
    mode = "--obs" if obs_running() else "--paused"
    log(f"запускаю бота ({mode})")
    # BREAKAWAY: бот не прив'язаний до процесу панелі — його коректно закриває сама панель
    flags = NO_WINDOW | DETACHED | (CREATE_BREAKAWAY_FROM_JOB if IS_WIN else 0)
    subprocess.Popen([str(py), str(ROOT / "bot.py"), mode], cwd=ROOT, creationflags=flags, close_fds=True,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def shutdown_bot(port: int, key: str) -> None:
    """Акуратно вимикає бота: він зупиняє музику, повертає трек у чергу й зберігає її."""
    if not bot_running(port):
        return
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/api/control",
        data=json.dumps({"action": "shutdown"}).encode(),
        headers={"Content-Type": "application/json", "X-Pipabobik-Key": key},
        method="POST",
    )
    try:
        urllib.request.urlopen(req, timeout=3).read()
    except OSError as e:
        log(f"shutdown: {e}")
    wait_until(lambda: not bot_running(port), 8)


# ── один екземпляр ──
_lock_sock: socket.socket | None = None


def single_instance(port: int) -> bool:
    global _lock_sock
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", port + 1))
        s.listen(1)
    except OSError:
        s.close()
        return False
    _lock_sock = s
    return True


def focus_existing() -> None:
    if not IS_WIN:
        return
    try:
        import ctypes

        u32 = ctypes.windll.user32
        hwnd = u32.FindWindowW(None, TITLE)
        if hwnd:
            u32.ShowWindow(hwnd, 9)  # SW_RESTORE
            u32.SetForegroundWindow(hwnd)
    except Exception:  # noqa: BLE001
        pass


def set_app_id() -> None:
    if IS_WIN:
        try:
            import ctypes

            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_ID)
        except Exception:  # noqa: BLE001
            pass


def set_window_icon() -> None:
    """Значок pipabobik на вікні та кнопці панелі задач."""
    if not IS_WIN or not ICON.exists():
        return
    try:
        import ctypes

        u32 = ctypes.windll.user32
        hwnd = 0
        for _ in range(40):
            hwnd = u32.FindWindowW(None, TITLE)
            if hwnd:
                break
            time.sleep(0.25)
        if not hwnd:
            return
        LR_LOADFROMFILE, IMAGE_ICON, WM_SETICON = 0x10, 1, 0x80
        big = u32.LoadImageW(None, str(ICON), IMAGE_ICON, 48, 48, LR_LOADFROMFILE)
        small = u32.LoadImageW(None, str(ICON), IMAGE_ICON, 16, 16, LR_LOADFROMFILE)
        if big:
            u32.SendMessageW(hwnd, WM_SETICON, 1, big)
        if small:
            u32.SendMessageW(hwnd, WM_SETICON, 0, small)
    except Exception:  # noqa: BLE001
        pass


def restarting() -> bool:
    """Бот перезапускається кнопкою в застосунку — тоді його коротка відсутність нормальна."""
    try:
        return time.time() - (DATA_DIR / "restarting").stat().st_mtime < 60
    except OSError:
        return False


# ── стеження: закрити панель разом з OBS / ботом ──
def watch(port: int, close, stop: threading.Event) -> None:
    obs_seen = obs_running()
    bot_seen = bot_running(port)
    bot_missing = 0
    while not stop.wait(3):
        if obs_running():
            obs_seen = True
        elif obs_seen:
            log("OBS закрито — закриваю панель")
            close()
            return
        if bot_running(port):
            bot_seen, bot_missing = True, 0
        elif bot_seen and not restarting():
            bot_missing += 1
            if bot_missing >= 2:  # бота закрили (кнопкою в панелі, з OBS-скрипта тощо)
                log("бот зупинився — закриваю панель")
                close()
                return


# ── вікно ──
def run_webview(url: str, port: int, key: str) -> bool:
    try:
        import webview  # pywebview
    except ImportError:
        return False
    stop = threading.Event()
    closed_by_us = {"v": False}
    window = webview.create_window(TITLE, url, width=480, height=900, min_size=(380, 560),
                                   background_color="#1c0716", text_select=False, confirm_close=True)

    def close() -> None:
        closed_by_us["v"] = True
        try:
            window.confirm_close = False  # закриваємо самі (OBS закрито) — без питання
            window.destroy()
        except Exception:  # noqa: BLE001
            pass

    def on_start() -> None:
        threading.Thread(target=set_window_icon, daemon=True).start()
        threading.Thread(target=watch, args=(port, close, stop), daemon=True).start()

    try:
        webview.start(on_start, private_mode=False, storage_path=str(DATA_DIR / "panel-webview"), localization={
            "global.quitConfirmation": "Закрити pipabobik? Музика зупиниться, черга збережеться.",
            "global.ok": "Так", "global.quit": "Закрити", "global.cancel": "Скасувати",
            "global.saveFile": "Зберегти файл", "windows.fileFilter.allFiles": "Усі файли",
        })
    except Exception as e:  # noqa: BLE001
        log(f"pywebview не запустився: {e}")
        return False
    stop.set()
    if not closed_by_us["v"]:
        log("вікно закрито користувачем — вимикаю pipabobik")
        shutdown_bot(port, key)
    return True


def find_browser() -> str | None:
    if not IS_WIN:
        for name in ("microsoft-edge", "google-chrome", "chromium"):
            if shutil.which(name):
                return shutil.which(name)
        return None
    for env in ("ProgramFiles(x86)", "ProgramFiles", "LOCALAPPDATA"):
        base = os.environ.get(env)
        if not base:
            continue
        for rel in ("Microsoft/Edge/Application/msedge.exe", "Google/Chrome/Application/chrome.exe"):
            if (Path(base) / rel).exists():
                return str(Path(base) / rel)
    return shutil.which("msedge") or shutil.which("chrome")


def run_browser_app(url: str, port: int, key: str) -> None:
    browser = find_browser()
    if not browser:
        webbrowser.open(url)
        return
    profile = DATA_DIR / "panel-window"
    proc = subprocess.Popen([
        browser, f"--app={url}", f"--user-data-dir={profile}", "--window-size=480,900",
        "--no-first-run", "--no-default-browser-check", "--disable-extensions", "--disable-sync",
        "--disable-background-networking", "--disable-session-crashed-bubble", "--hide-crash-restore-bubble",
        "--disable-features=Translate,msEdgeSidebarV2,msUndersideButton,msStartupBoost",
    ], close_fds=True, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    stop = threading.Event()
    closed_by_us = {"v": False}

    def close() -> None:
        closed_by_us["v"] = True
        proc.terminate()

    started = time.time()
    watcher = threading.Thread(target=watch, args=(port, close, stop), daemon=True)
    watcher.start()
    proc.wait()
    if time.time() - started < 5 and not closed_by_us["v"]:
        # браузер передав вікно вже запущеному екземпляру — не вважаємо це закриттям
        log("браузер передав вікно іншому процесу; лише стежу за OBS і ботом")
        watcher.join()
        return
    stop.set()
    if not closed_by_us["v"]:
        log("вікно закрито користувачем — вимикаю pipabobik")
        shutdown_bot(port, key)


def main() -> None:
    cfg = load_config()
    port = cfg.port
    if not single_instance(port):
        focus_existing()
        return
    set_app_id()
    kill_children_with_me()  # вікно браузера не переживе панель
    # OBS-скрипт міг щойно запустити бота — даємо йому кілька секунд
    if not wait_until(lambda: bot_running(port), 6):
        start_bot()
        wait_until(lambda: bot_running(port), 20)
    key_file = DATA_DIR / "panel.key"
    wait_until(key_file.exists, 5, 0.25)
    key = key_file.read_text().strip() if key_file.exists() else ""
    url = f"http://localhost:{port}/app?key={key}"
    log("відкриваю панель")
    if not run_webview(url, port, key):
        run_browser_app(url, port, key)


if __name__ == "__main__":
    main()
