#!/usr/bin/env python3
"""Run the local STW Intelligence watcher, API, and minimal dashboard."""

from __future__ import annotations

import argparse
import atexit
import ctypes
import json
import os
import sqlite3
import threading
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable
from urllib.parse import parse_qs, urlparse

from stw_admin import (
    ApplicationLock,
    backup_database,
    diagnostics,
    get_settings,
    preflight_settings,
    prune_history,
    sanitize_live_spools,
    update_settings,
)
from stw_activity import (
    activity_overview,
    cohort_catalog,
    recommendation_overview,
    refresh_activity,
)
from stw_ai import AiOrchestrator, StwAiTools
from stw_live import LogWatcher
from stw_pipeline import connect
from stw_providers import (
    FixtureProvider,
    HttpMissionProvider,
    MissionProvider,
    ingest_provider_rotation,
    match_rotation,
)
from stw_queries import (
    application_health,
    attempt_detail,
    current_correlations,
    current_missions,
    current_state,
    recent_attempts,
)


ROOT = Path(__file__).resolve().parents[1]
DASHBOARD = ROOT / "web" / "index.html"
DEFAULT_FIXTURE = ROOT / "fixtures" / "current-mission-rotation.json"


def process_exists(pid: int) -> bool:
    """Check a process without requiring third-party packages."""
    if pid <= 0:
        return False
    if os.name == "nt":
        process_query_limited_information = 0x1000
        still_active = 259
        kernel32 = ctypes.windll.kernel32
        kernel32.OpenProcess.argtypes = [
            ctypes.c_ulong,
            ctypes.c_int,
            ctypes.c_ulong,
        ]
        kernel32.OpenProcess.restype = ctypes.c_void_p
        kernel32.GetExitCodeProcess.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_ulong),
        ]
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        handle = kernel32.OpenProcess(
            process_query_limited_information, False, pid
        )
        if not handle:
            return kernel32.GetLastError() == 5  # Access denied means it exists.
        try:
            exit_code = ctypes.c_ulong()
            return bool(kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code))) and (
                exit_code.value == still_active
            )
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True
    except OSError:
        return False


class ProcessLifetimeMonitor:
    def __init__(
        self,
        pid: int,
        on_exit: Callable[[], None],
        checker: Callable[[int], bool] = process_exists,
        poll_interval: float = 1.0,
    ):
        self.pid = pid
        self.on_exit = on_exit
        self.checker = checker
        self.poll_interval = poll_interval
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while not self._stop.wait(self.poll_interval):
            if not self.checker(self.pid):
                self.on_exit()
                return

    def stop(self) -> None:
        self._stop.set()
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout=self.poll_interval + 1.0)


class ApiApplication:
    def __init__(
        self,
        database: Path,
        dashboard: Path = DASHBOARD,
        provider_status: Callable[[], dict[str, object]] | None = None,
        active_log: Path | None = None,
    ):
        self.database = database.resolve()
        self.dashboard = dashboard
        self.provider_status = provider_status
        self.active_log = active_log.resolve() if active_log else None

    def dispatch(
        self, method: str, target: str, body: bytes = b""
    ) -> tuple[int, str, bytes]:
        if method not in ("GET", "POST"):
            return self._json(HTTPStatus.METHOD_NOT_ALLOWED, {"error": "unsupported method"})
        parsed = urlparse(target)
        if method == "GET" and parsed.path == "/":
            return HTTPStatus.OK, "text/html; charset=utf-8", self.dashboard.read_bytes()
        connection = connect(self.database)
        try:
            if method == "POST":
                try:
                    payload = json.loads(body or b"{}")
                except (json.JSONDecodeError, UnicodeDecodeError):
                    return self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid JSON"})
                if not isinstance(payload, dict):
                    return self._json(HTTPStatus.BAD_REQUEST, {"error": "JSON object required"})
                if parsed.path == "/api/settings":
                    try:
                        result = update_settings(connection, payload, self.active_log)
                    except ValueError as error:
                        return self._json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
                    return self._json(
                        HTTPStatus.OK,
                        {
                            **result,
                            "settings": get_settings(
                                connection, self.database, self.active_log
                            ),
                        },
                    )
                if parsed.path == "/api/admin/backup":
                    settings = get_settings(connection)
                    result = backup_database(
                        self.database,
                        self.database.parent / "backups",
                        settings["backup_keep"],
                    )
                    return self._json(HTTPStatus.OK, result)
                if parsed.path == "/api/admin/retention":
                    if payload.get("confirm") is not True:
                        return self._json(
                            HTTPStatus.BAD_REQUEST,
                            {"error": "retention cleanup requires confirmation"},
                        )
                    settings = get_settings(connection)
                    result = prune_history(
                        connection,
                        settings["history_retention_days"],
                        confirmed=True,
                    )
                    refresh_activity(connection)
                    return self._json(HTTPStatus.OK, result)
                if parsed.path == "/api/ai/recommend":
                    request = payload.get("request")
                    intent = payload.get("intent")
                    if not isinstance(request, str) or not request.strip():
                        return self._json(
                            HTTPStatus.BAD_REQUEST,
                            {"error": "request must be a non-empty string"},
                        )
                    if intent is not None and not isinstance(intent, dict):
                        return self._json(
                            HTTPStatus.BAD_REQUEST,
                            {"error": "intent must be an object"},
                        )
                    try:
                        result = AiOrchestrator(StwAiTools(connection)).run(
                            request.strip(), intent
                        )
                    except ValueError as error:
                        return self._json(
                            HTTPStatus.BAD_REQUEST, {"error": str(error)}
                        )
                    return self._json(HTTPStatus.OK, result)
                return self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            if parsed.path == "/api/ai/tools":
                return self._json(HTTPStatus.OK, StwAiTools.schemas())
            if parsed.path == "/api/current":
                return self._json(HTTPStatus.OK, current_state(connection))
            if parsed.path == "/api/attempts":
                raw_limit = parse_qs(parsed.query).get("limit", ["20"])[0]
                try:
                    limit = int(raw_limit)
                except ValueError:
                    return self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid limit"})
                return self._json(HTTPStatus.OK, {"attempts": recent_attempts(connection, limit)})
            if parsed.path.startswith("/api/attempts/"):
                try:
                    attempt_id = int(parsed.path.rsplit("/", 1)[1])
                except ValueError:
                    return self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid attempt id"})
                result = attempt_detail(connection, attempt_id)
                return self._json(
                    HTTPStatus.OK if result else HTTPStatus.NOT_FOUND,
                    result or {"error": "attempt not found"},
                )
            if parsed.path == "/api/missions/current":
                return self._json(HTTPStatus.OK, current_missions(connection))
            if parsed.path == "/api/correlation/current":
                return self._json(
                    HTTPStatus.OK, {"correlations": current_correlations(connection)}
                )
            if parsed.path == "/api/activity/current":
                raw_node = parse_qs(parsed.query).get("mission_node", [None])[0]
                try:
                    mission_node_id = int(raw_node) if raw_node is not None else None
                except ValueError:
                    return self._json(
                        HTTPStatus.BAD_REQUEST, {"error": "invalid mission node"}
                    )
                return self._json(
                    HTTPStatus.OK, activity_overview(connection, mission_node_id)
                )
            if parsed.path == "/api/recommendation/current":
                query = parse_qs(parsed.query)
                raw_node = query.get("mission_node", [None])[0]
                try:
                    mission_node_id = int(raw_node) if raw_node is not None else None
                except ValueError:
                    return self._json(
                        HTTPStatus.BAD_REQUEST, {"error": "invalid mission node"}
                    )
                raw_at = query.get("at", [None])[0]
                try:
                    at = (
                        datetime.fromisoformat(raw_at.replace("Z", "+00:00"))
                        if raw_at
                        else None
                    )
                except ValueError:
                    return self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid at"})
                settings = get_settings(connection)
                timezone_name = query.get("timezone", [settings["timezone"]])[0]
                try:
                    result = recommendation_overview(
                        connection, mission_node_id, at, timezone_name
                    )
                except ValueError as error:
                    return self._json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
                return self._json(HTTPStatus.OK, result)
            if parsed.path == "/api/cohorts/current":
                return self._json(HTTPStatus.OK, cohort_catalog(connection))
            if parsed.path == "/api/settings":
                return self._json(
                    HTTPStatus.OK,
                    get_settings(connection, self.database, self.active_log),
                )
            if parsed.path == "/api/diagnostics":
                runtime = self.provider_status() if self.provider_status else None
                return self._json(
                    HTTPStatus.OK,
                    diagnostics(
                        connection,
                        self.database,
                        self.active_log,
                        self.dashboard,
                        runtime,
                    ),
                )
            if parsed.path == "/api/health":
                health = application_health(connection)
                if self.provider_status is not None:
                    health["provider_runtime"] = self.provider_status()
                return self._json(HTTPStatus.OK, health)
            return self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
        finally:
            connection.close()

    @staticmethod
    def _json(status: int, value: object) -> tuple[int, str, bytes]:
        return (
            int(status),
            "application/json; charset=utf-8",
            json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
        )


def handler_for(application: ApiApplication) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            status, content_type, body = self._dispatch("GET")
            self._send(status, content_type, body)

        def do_POST(self) -> None:  # noqa: N802
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                length = 0
            if length > 65536:
                status, content_type, body = application._json(
                    HTTPStatus.REQUEST_ENTITY_TOO_LARGE, {"error": "request too large"}
                )
            else:
                status, content_type, body = self._dispatch(
                    "POST", self.rfile.read(length)
                )
            self._send(status, content_type, body)

        def _dispatch(
            self, method: str, body: bytes = b""
        ) -> tuple[int, str, bytes]:
            try:
                return application.dispatch(method, self.path, body)
            except Exception as error:
                print(f"API error: {type(error).__name__}: {error}")
                return application._json(
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                    {"error": "local application error; check diagnostics"},
                )

        def _send(self, status: int, content_type: str, body: bytes) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            print(f"HTTP {self.address_string()} {format % args}")

    return Handler


def _default_log() -> Path | None:
    local_app_data = os.environ.get("LOCALAPPDATA")
    if not local_app_data:
        return None
    return Path(local_app_data) / "FortniteGame" / "Saved" / "Logs" / "FortniteGame.log"


class ProviderRefreshLoop:
    def __init__(
        self, database: Path, provider: MissionProvider, refresh_seconds: float = 300.0
    ) -> None:
        self.database = database.resolve()
        self.provider = provider
        self.refresh_seconds = max(5.0, refresh_seconds)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._status: dict[str, object] = {
            "status": "unknown",
            "freshness": "unknown",
            "detail": "not refreshed",
            "last_attempt_at": None,
            "last_success_at": None,
        }

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    def refresh_once(self) -> dict[str, object]:
        attempted_at = self._now()
        connection = connect(self.database)
        try:
            ingestion = ingest_provider_rotation(connection, self.provider)
            matching = match_rotation(connection, ingestion["rotation_id"])
            activity = refresh_activity(connection)
            health = self.provider.health()
            status: dict[str, object] = {
                **health.__dict__,
                "last_attempt_at": attempted_at,
                "last_success_at": self._now(),
                "rotation_id": ingestion["rotation_id"],
                "ingestion": ingestion,
                "matching": matching,
                "activity": activity,
            }
        except Exception as error:  # keep the last good database rotation available
            health = self.provider.health()
            status = {
                **health.__dict__,
                "status": "unhealthy",
                "last_attempt_at": attempted_at,
                "last_success_at": self.status().get("last_success_at"),
                "error": f"{type(error).__name__}: {error}",
            }
        finally:
            connection.close()
        with self._lock:
            self._status = status
        return dict(status)

    def status(self) -> dict[str, object]:
        with self._lock:
            return dict(self._status)

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(
            target=self._run, name="stw-provider-refresh", daemon=True
        )
        self._thread.start()

    def _run(self) -> None:
        while not self._stop.wait(self._next_wait_seconds()):
            self.refresh_once()

    def _next_wait_seconds(self, now: datetime | None = None) -> float:
        state = self.status()
        valid_until = state.get("valid_until")
        if state.get("status") == "healthy" and isinstance(valid_until, str):
            try:
                expiry = datetime.fromisoformat(valid_until.replace("Z", "+00:00"))
                instant = now or datetime.now(timezone.utc)
                if instant.tzinfo is None:
                    instant = instant.replace(tzinfo=timezone.utc)
                return max(5.0, (expiry - instant).total_seconds() + 30.0)
            except ValueError:
                pass
        return self.refresh_seconds

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=25.0)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=Path("data/stw-intelligence.sqlite3"))
    parser.add_argument("--log", type=Path)
    parser.add_argument("--fixture", type=Path, default=DEFAULT_FIXTURE)
    parser.add_argument("--provider-url", help="approved normalized mission feed URL")
    parser.add_argument("--provider-code", default="configured_http_feed")
    parser.add_argument("--provider-name", default="Configured HTTPS mission feed")
    parser.add_argument("--provider-terms-url")
    parser.add_argument("--provider-api-key-env")
    parser.add_argument("--provider-api-key-header", default="API-Key")
    parser.add_argument("--provider-refresh-seconds", type=float, default=300.0)
    parser.add_argument(
        "--allow-http-provider",
        action="store_true",
        help="allow an insecure HTTP feed for local development only",
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument(
        "--from-start",
        action="store_true",
        help="process an existing log from byte zero on its first watch",
    )
    parser.add_argument(
        "--exit-when-process-exits",
        type=int,
        metavar="PID",
        help=argparse.SUPPRESS,
    )
    args = parser.parse_args()
    instance_lock = ApplicationLock(args.db.parent / "stw_app.lock")
    try:
        instance_lock.acquire()
    except RuntimeError as error:
        print(error)
        print("Use the existing dashboard window or stop it with Ctrl+C first.")
        return 2
    atexit.register(instance_lock.release)
    before_connect = preflight_settings(args.db)
    if before_connect["auto_backup_on_start"]:
        try:
            backup = backup_database(
                args.db, args.db.parent / "backups", before_connect["backup_keep"]
            )
            if backup["status"] == "created":
                print(f"Database backup: {backup['path']}")
        except (OSError, sqlite3.Error) as error:
            print(f"Database backup warning: {error}")

    setup_connection = connect(args.db)
    try:
        stored = get_settings(setup_connection)
        spool_result = sanitize_live_spools(setup_connection)
        if spool_result["sanitized"]:
            print(f"Sanitized {spool_result['sanitized']} local live spool(s).")
        configured_log = stored["configured_log_path"]
        log_path = args.log or (Path(configured_log) if configured_log else _default_log())
        if args.log is not None:
            update_settings(
                setup_connection,
                {"configured_log_path": str(args.log)},
                args.log,
            )
        if stored["history_retention_days"] > 0:
            cleanup = prune_history(
                setup_connection,
                stored["history_retention_days"],
                confirmed=True,
            )
            if cleanup["deleted"]:
                print(f"History retention removed {cleanup['deleted']} old attempt(s).")
    finally:
        setup_connection.close()
    if log_path is None:
        parser.error("--log is required when LOCALAPPDATA is unavailable")

    provider_loop: ProviderRefreshLoop | None = None
    provider_status: Callable[[], dict[str, object]] | None = None
    if args.provider_url:
        try:
            provider = HttpMissionProvider(
                args.provider_url,
                code=args.provider_code,
                display_name=args.provider_name,
                terms_url=args.provider_terms_url,
                api_key_env=args.provider_api_key_env,
                api_key_header=args.provider_api_key_header,
                allow_insecure_http=args.allow_http_provider,
            )
        except ValueError as error:
            parser.error(str(error))
        provider_loop = ProviderRefreshLoop(
            args.db, provider, args.provider_refresh_seconds
        )
        initial_status = provider_loop.refresh_once()
        provider_status = provider_loop.status
        if initial_status["status"] != "healthy":
            print(f"Mission provider warning: {initial_status.get('error') or initial_status['detail']}")
        provider_loop.start()
    else:
        fixture_provider = FixtureProvider(args.fixture)
        connection = connect(args.db)
        try:
            if args.fixture and args.fixture.exists():
                ingestion = ingest_provider_rotation(connection, fixture_provider)
                match_rotation(connection, ingestion["rotation_id"])
            refresh_activity(connection)
        finally:
            connection.close()
        provider_status = lambda: fixture_provider.health().__dict__

    try:
        server = ThreadingHTTPServer(
            (args.host, args.port),
            handler_for(
                ApiApplication(
                    args.db, provider_status=provider_status, active_log=log_path
                )
            ),
        )
    except OSError as error:
        if provider_loop is not None:
            provider_loop.stop()
        print(f"Unable to start local server on {args.host}:{args.port}: {error}")
        print("Close the other STW Intelligence window or choose a different --port.")
        return 2
    watcher = LogWatcher(
        args.db,
        log_path,
        poll_interval=0.5,
        start_at_end=not args.from_start,
    )
    watcher.start()
    lifetime_monitor = None
    if args.exit_when_process_exits is not None:
        lifetime_monitor = ProcessLifetimeMonitor(
            args.exit_when_process_exits, server.shutdown
        )
        lifetime_monitor.start()
    print(f"STW Intelligence: http://{args.host}:{args.port}")
    print(f"Watching: {log_path.resolve()}")
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        print("Stopping STW Intelligence...")
    finally:
        server.shutdown()
        server.server_close()
        watcher.stop()
        if lifetime_monitor is not None:
            lifetime_monitor.stop()
        if provider_loop is not None:
            provider_loop.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
