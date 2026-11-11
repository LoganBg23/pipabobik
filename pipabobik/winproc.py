"""Керування дочірніми процесами на Windows.

kill_children_with_me(): поміщає поточний процес у Job Object з прапорцем
KILL_ON_JOB_CLOSE. Усі процеси, які він запустить (mpv, Deno, вікно панелі),
гарантовано закриються разом із ним — навіть якщо його вбили через Диспетчер завдань.
"""
from __future__ import annotations

import logging
import sys

log = logging.getLogger("pipabobik.winproc")
_JOB = None  # тримаємо дескриптор, поки живе процес

CREATE_BREAKAWAY_FROM_JOB = 0x01000000


def kill_children_with_me() -> bool:
    global _JOB
    if sys.platform != "win32" or _JOB is not None:
        return False
    try:
        import ctypes
        from ctypes import wintypes

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)

        class IO_COUNTERS(ctypes.Structure):
            _fields_ = [(n, ctypes.c_ulonglong) for n in (
                "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

        class BASIC(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD), ("SchedulingClass", wintypes.DWORD),
            ]

        class EXTENDED(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", BASIC), ("IoInfo", IO_COUNTERS),
                ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        k32.CreateJobObjectW.restype = wintypes.HANDLE
        k32.GetCurrentProcess.restype = wintypes.HANDLE
        job = k32.CreateJobObjectW(None, None)
        if not job:
            return False
        info = EXTENDED()
        # KILL_ON_JOB_CLOSE | BREAKAWAY_OK (щоб за потреби процес міг запустити щось незалежне)
        info.BasicLimitInformation.LimitFlags = 0x2000 | 0x0800
        if not k32.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info)):
            return False
        if not k32.AssignProcessToJobObject(job, k32.GetCurrentProcess()):
            log.debug("AssignProcessToJobObject: %s", ctypes.get_last_error())
            return False
        _JOB = job
        return True
    except Exception as e:  # noqa: BLE001
        log.debug("Job Object недоступний: %s", e)
        return False


def kill_orphan_players() -> int:
    """Закриває mpv від попереднього запуску бота (якщо той аварійно завершився)."""
    try:
        import psutil
    except ImportError:
        return 0
    n = 0
    for p in psutil.process_iter(["name", "cmdline"]):
        try:
            name = (p.info.get("name") or "").lower()
            cmd = " ".join(p.info.get("cmdline") or [])
            if "mpv" in name and "pipabobik-mpv" in cmd:
                p.kill()
                n += 1
        except (psutil.Error, OSError):
            continue
    return n
