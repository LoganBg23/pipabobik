"""Відтворення через mpv: окремий процес на кожен трек + IPC для паузи й гучності.

В OBS музика захоплюється джерелом «Захоплення звуку програми» → mpv.exe.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

log = logging.getLogger("pipabobik.player")

IS_WIN = sys.platform == "win32"


def find_mpv(configured: str = "mpv") -> str | None:
    if configured and configured != "mpv" and Path(configured).exists():
        return configured
    found = shutil.which("mpv")
    if found:
        return found
    candidates: list[Path] = []
    if IS_WIN:
        bases = [os.environ.get(e) for e in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA", "USERPROFILE")]
        for base in filter(None, bases):
            b = Path(base)
            candidates += [b / "mpv" / "mpv.exe", b / "scoop" / "shims" / "mpv.exe"]
            # «MPV Player», «mpv.net», «Programs\mpv» тощо
            candidates += sorted(b.glob("*mpv*/mpv.exe")) + sorted(b.glob("Programs/*mpv*/mpv.exe"))
        la = os.environ.get("LOCALAPPDATA")
        if la:  # winget: посилання та розпаковані пакети
            winget = Path(la) / "Microsoft" / "WinGet"
            candidates.append(winget / "Links" / "mpv.exe")
            candidates += sorted(winget.glob("Packages/*mpv*/**/mpv.exe"))
    else:
        candidates += [Path("/opt/homebrew/bin/mpv"), Path("/usr/local/bin/mpv"), Path("/usr/bin/mpv")]
    for c in candidates:
        if c.exists():
            return str(c)
    return None


class Player:
    def __init__(self, mpv_path: str, volume: int = 60, normalize: bool = True, audio_device: str = "",
                 show_window: bool = True):
        self.mpv = find_mpv(mpv_path)
        # OBS «Захоплення звуку програми» бачить лише програми з вікном,
        # тому на Windows mpv відкривається згорнутим вікном «pipabobik».
        self.show_window = show_window and IS_WIN
        self.volume = max(0, min(100, int(volume)))
        self.normalize = normalize
        self.audio_device = audio_device
        self.proc: asyncio.subprocess.Process | None = None
        self.paused = False
        self._ipc = r"\\.\pipe\pipabobik-mpv" if IS_WIN else f"/tmp/pipabobik-mpv-{os.getuid()}.sock"
        # облік позиції
        self._started = 0.0
        self._paused_total = 0.0
        self._paused_since: float | None = None
        self._stopped_by_us = False

    @property
    def available(self) -> bool:
        return self.mpv is not None

    @property
    def position(self) -> float:
        if not self._started:
            return 0.0
        now = self._paused_since or time.monotonic()
        return max(0.0, now - self._started - self._paused_total)

    def _cmd(self, url: str, headers: dict) -> list[str]:
        cmd = [
            self.mpv,
            "--no-video",
            "--no-terminal",
            *(["--force-window=yes", "--window-minimized=yes", "--geometry=320x80"] if self.show_window
              else ["--force-window=no"]),
            "--audio-display=no",
            "--idle=no",
            "--title=pipabobik",
            f"--volume={self.volume}",
            f"--input-ipc-server={self._ipc}",
            # нічого зайвого: без конфігів, скриптів, OSD та ytdl-хука (посилання вже пряме)
            "--config=no",
            "--load-scripts=no",
            "--osc=no",
            "--ytdl=no",
            "--input-default-bindings=no",
            "--cache=yes",
            "--demuxer-max-bytes=16MiB",
            "--demuxer-readahead-secs=30",
        ]
        if self.normalize:
            # dynaudnorm працює на рідній частоті — у рази легший за loudnorm (той передискретизує до 192 кГц)
            cmd.append("--af=lavfi=[dynaudnorm=f=300:g=15:p=0.9:m=6]")
        if self.audio_device:
            cmd.append(f"--audio-device={self.audio_device}")
        for k, v in headers.items():
            if k.lower() == "user-agent":
                cmd.append(f"--user-agent={v}")
            elif k.lower() not in ("accept-encoding",):
                cmd.append(f"--http-header-fields-append={k}: {v}")
        cmd.append(url)
        return cmd

    async def play(self, url: str, headers: dict | None = None) -> str:
        """Грає трек до кінця. Повертає 'ended' | 'stopped' | 'error'."""
        if not self.mpv:
            log.error("mpv не знайдено — встановіть його (див. README)")
            await asyncio.sleep(5)
            return "error"
        await self.stop()
        kwargs = {}
        if IS_WIN:
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        self._stopped_by_us = False
        self.paused = False
        self._paused_total, self._paused_since = 0.0, None
        self._started = time.monotonic()
        self.proc = await asyncio.create_subprocess_exec(
            *self._cmd(url, headers or {}),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
            **kwargs,
        )
        _, err = await self.proc.communicate()
        rc = self.proc.returncode
        played = self.position
        self.proc = None
        self._started = 0.0
        if self._stopped_by_us:
            return "stopped"
        if rc != 0 and played < 3:
            log.warning("mpv завершився з кодом %s: %s", rc, (err or b"").decode(errors="ignore")[-300:])
            return "error"
        return "ended"

    async def stop(self) -> None:
        p = self.proc
        if p and p.returncode is None:
            self._stopped_by_us = True
            try:
                p.terminate()
                await asyncio.wait_for(p.wait(), timeout=3)
            except (ProcessLookupError, asyncio.TimeoutError):
                try:
                    p.kill()
                except ProcessLookupError:
                    pass

    # ── IPC ──
    def _send_ipc(self, *command) -> bool:
        payload = (json.dumps({"command": list(command)}) + "\n").encode()
        try:
            if IS_WIN:
                with open(self._ipc, "r+b", buffering=0) as pipe:
                    pipe.write(payload)
            else:
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
                    s.settimeout(1)
                    s.connect(self._ipc)
                    s.sendall(payload)
            return True
        except OSError as e:
            log.debug("IPC mpv недоступний: %s", e)
            return False

    async def set_volume(self, vol: int) -> None:
        self.volume = max(0, min(100, int(vol)))
        if self.proc:
            await asyncio.to_thread(self._send_ipc, "set_property", "volume", self.volume)

    async def seek(self, position: float) -> bool:
        """Перемотує на позицію в секундах від початку треку."""
        if not self.proc:
            return False
        position = max(0.0, float(position))
        if not await asyncio.to_thread(self._send_ipc, "seek", position, "absolute"):
            return False
        # підлаштовуємо власний облік позиції під нову точку
        now = self._paused_since or time.monotonic()
        self._started = now - position - self._paused_total
        return True

    async def set_pause(self, paused: bool) -> None:
        if not self.proc or paused == self.paused:
            return
        if await asyncio.to_thread(self._send_ipc, "set_property", "pause", paused):
            self.paused = paused
            if paused:
                self._paused_since = time.monotonic()
            elif self._paused_since:
                self._paused_total += time.monotonic() - self._paused_since
                self._paused_since = None
