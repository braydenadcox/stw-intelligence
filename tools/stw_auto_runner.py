#!/usr/bin/env python3
"""Start STW Intelligence only while the Fortnite client is running."""

from __future__ import annotations

import ctypes
import subprocess
import sys
import time
import urllib.request
import webbrowser
from pathlib import Path
from typing import Callable
from ctypes import wintypes

from stw_admin import ApplicationLock


ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "tools" / "stw_app.py"
FORTNITE_IMAGE = "FortniteClient-Win64-Shipping.exe"
HEALTH_URL = "http://127.0.0.1:8765/api/health"
DASHBOARD_URL = "http://127.0.0.1:8765"
POLL_SECONDS = 5.0
STOP_FILE = ROOT / "data" / "stw_auto_runner.stop"
LOCK_FILE = ROOT / "data" / "stw_auto_runner.lock"


def fortnite_process_ids() -> list[int]:
    """Return live Fortnite client PIDs without opening protected game processes."""
    if sys.platform != "win32":
        return []
    class ProcessEntry(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.c_void_p),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", wintypes.LONG),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", wintypes.WCHAR * 260),
        ]

    kernel32 = ctypes.windll.kernel32
    kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel32.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
    kernel32.Process32FirstW.argtypes = [ctypes.c_void_p, ctypes.POINTER(ProcessEntry)]
    kernel32.Process32NextW.argtypes = [ctypes.c_void_p, ctypes.POINTER(ProcessEntry)]
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    snapshot = kernel32.CreateToolhelp32Snapshot(0x00000002, 0)
    if snapshot == ctypes.c_void_p(-1).value:
        return []
    pids: list[int] = []
    entry = ProcessEntry()
    entry.dwSize = ctypes.sizeof(ProcessEntry)
    try:
        found = kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
        while found:
            if entry.szExeFile.casefold() == FORTNITE_IMAGE.casefold():
                pids.append(int(entry.th32ProcessID))
            found = kernel32.Process32NextW(snapshot, ctypes.byref(entry))
        return pids
    finally:
        kernel32.CloseHandle(snapshot)


def app_is_running(timeout: float = 0.5) -> bool:
    try:
        with urllib.request.urlopen(HEALTH_URL, timeout=timeout) as response:
            return response.status == 200
    except OSError:
        return False


def open_dashboard_url(url: str) -> bool:
    """Open a URL through the interactive Windows shell, with a portable fallback."""
    if sys.platform == "win32":
        result = ctypes.windll.shell32.ShellExecuteW(
            None, "open", url, None, str(ROOT), 1
        )
        if int(result) > 32:
            return True
    return bool(webbrowser.open(url))


def wait_until_ready(
    child: subprocess.Popen[bytes], timeout: float = 30.0
) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if child.poll() is not None:
            return False
        if app_is_running():
            return True
        time.sleep(0.25)
    return False


def launch_for_pid(pid: int) -> subprocess.Popen[bytes]:
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return subprocess.Popen(
        [sys.executable, str(APP), "--exit-when-process-exits", str(pid)],
        cwd=ROOT,
        creationflags=flags,
    )


def supervise_once(
    process_ids: Callable[[], list[int]] = fortnite_process_ids,
    launch: Callable[[int], subprocess.Popen[bytes]] = launch_for_pid,
    ready: Callable[[subprocess.Popen[bytes]], bool] = wait_until_ready,
    pause: Callable[[float], None] = time.sleep,
    open_dashboard: Callable[[str], object] = open_dashboard_url,
    app_running: Callable[[], bool] = app_is_running,
    stop_requested: Callable[[], bool] = STOP_FILE.exists,
    poll_seconds: float = POLL_SECONDS,
) -> str:
    """Supervise one Fortnite session; dependencies are injectable for tests."""
    pids = process_ids()
    if not pids:
        return "waiting"
    watched_pid = pids[0]
    if app_running():
        open_dashboard(DASHBOARD_URL)
        while process_ids() and not stop_requested():
            pause(poll_seconds)
        return "existing_app"
    child = launch(watched_pid)
    if ready(child):
        open_dashboard(DASHBOARD_URL)
    while (
        child.poll() is None
        and watched_pid in process_ids()
        and not stop_requested()
    ):
        pause(poll_seconds)
    if stop_requested():
        # The child independently watches Fortnite's PID and will shut itself down
        # when the game exits; disabling the lightweight runner must not kill it.
        return "monitor_stopped"
    # The application watches the same PID and exits gracefully. Wait briefly for
    # its watcher/server cleanup rather than terminating SQLite work mid-transaction.
    if child.poll() is None:
        try:
            child.wait(timeout=15.0)
        except subprocess.TimeoutExpired:
            child.terminate()
            child.wait(timeout=5.0)
    return "session_complete"


def main() -> int:
    lock = ApplicationLock(LOCK_FILE)
    try:
        lock.acquire()
    except RuntimeError:
        return 0
    try:
        while not STOP_FILE.exists():
            supervise_once()
            if STOP_FILE.exists():
                break
            time.sleep(POLL_SECONDS)
        return 0
    finally:
        lock.release()


if __name__ == "__main__":
    raise SystemExit(main())
