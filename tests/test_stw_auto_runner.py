from __future__ import annotations

import os
import subprocess
import sys
import threading
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
from stw_app import ProcessLifetimeMonitor, process_exists  # noqa: E402
from stw_auto_runner import (  # noqa: E402
    DASHBOARD_URL,
    ROOT as RUNNER_ROOT,
    open_dashboard_url,
    supervise_once,
)


class FakeChild:
    def __init__(self) -> None:
        self.returncode = None
        self.terminated = False

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        self.returncode = 0
        return 0

    def terminate(self):
        self.terminated = True
        self.returncode = 1


class AutoRunnerTests(unittest.TestCase):
    @patch("stw_auto_runner.subprocess.Popen")
    def test_browser_uses_interactive_windows_shell(self, popen) -> None:
        self.assertTrue(open_dashboard_url(DASHBOARD_URL))
        popen.assert_called_once_with(
            ["explorer.exe", DASHBOARD_URL],
            cwd=RUNNER_ROOT,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )

    def test_waits_without_launching_when_fortnite_is_closed(self) -> None:
        launched = []
        result = supervise_once(
            process_ids=lambda: [],
            launch=lambda pid: launched.append(pid),
            app_running=lambda: False,
            stop_requested=lambda: False,
        )
        self.assertEqual("waiting", result)
        self.assertEqual([], launched)

    def test_launches_once_opens_dashboard_and_waits_for_clean_exit(self) -> None:
        process_states = iter(([42], [42], []))
        child = FakeChild()
        opened = []
        launched = []
        result = supervise_once(
            process_ids=lambda: list(next(process_states)),
            launch=lambda pid: launched.append(pid) or child,
            ready=lambda process: process is child,
            pause=lambda seconds: None,
            open_dashboard=opened.append,
            app_running=lambda: False,
            stop_requested=lambda: False,
            poll_seconds=0,
        )
        self.assertEqual("session_complete", result)
        self.assertEqual([42], launched)
        self.assertEqual([DASHBOARD_URL], opened)
        self.assertFalse(child.terminated)
        self.assertEqual(0, child.returncode)

    def test_does_not_control_an_app_started_manually(self) -> None:
        process_states = iter(([42], [42], []))
        launched = []
        result = supervise_once(
            process_ids=lambda: list(next(process_states)),
            launch=lambda pid: launched.append(pid),
            pause=lambda seconds: None,
            app_running=lambda: True,
            open_dashboard=lambda url: None,
            stop_requested=lambda: False,
            poll_seconds=0,
        )
        self.assertEqual("existing_app", result)
        self.assertEqual([], launched)

    def test_process_lifetime_monitor_requests_graceful_shutdown(self) -> None:
        states = iter((True, False))
        stopped = threading.Event()
        monitor = ProcessLifetimeMonitor(
            42,
            stopped.set,
            checker=lambda pid: next(states),
            poll_interval=0.01,
        )
        monitor.start()
        try:
            self.assertTrue(stopped.wait(timeout=1.0))
        finally:
            monitor.stop()

    def test_process_exists_handles_live_and_missing_pids(self) -> None:
        self.assertTrue(process_exists(os.getpid()))
        self.assertFalse(process_exists(2_147_483_647))


if __name__ == "__main__":
    unittest.main()
