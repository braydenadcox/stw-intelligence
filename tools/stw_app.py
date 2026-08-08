#!/usr/bin/env python3
"""Run the local STW Intelligence watcher, API, and minimal dashboard."""

from __future__ import annotations

import argparse
import json
import os
import threading
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable
from urllib.parse import parse_qs, urlparse

from stw_activity import activity_overview, recommendation_overview, refresh_activity
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


class ApiApplication:
    def __init__(
        self,
        database: Path,
        dashboard: Path = DASHBOARD,
        provider_status: Callable[[], dict[str, object]] | None = None,
    ):
        self.database = database.resolve()
        self.dashboard = dashboard
        self.provider_status = provider_status

    def dispatch(self, method: str, target: str) -> tuple[int, str, bytes]:
        if method != "GET":
            return self._json(HTTPStatus.METHOD_NOT_ALLOWED, {"error": "GET only"})
        parsed = urlparse(target)
        if parsed.path == "/":
            return HTTPStatus.OK, "text/html; charset=utf-8", self.dashboard.read_bytes()
        connection = connect(self.database)
        try:
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
                raw_node = parse_qs(parsed.query).get("mission_node", [None])[0]
                try:
                    mission_node_id = int(raw_node) if raw_node is not None else None
                except ValueError:
                    return self._json(
                        HTTPStatus.BAD_REQUEST, {"error": "invalid mission node"}
                    )
                return self._json(
                    HTTPStatus.OK, recommendation_overview(connection, mission_node_id)
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
            status, content_type, body = application.dispatch("GET", self.path)
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
    parser.add_argument("--log", type=Path, default=_default_log())
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
    args = parser.parse_args()
    if args.log is None:
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

    watcher = LogWatcher(
        args.db,
        args.log,
        poll_interval=0.5,
        start_at_end=not args.from_start,
    )
    watcher.start()
    server = ThreadingHTTPServer(
        (args.host, args.port),
        handler_for(ApiApplication(args.db, provider_status=provider_status)),
    )
    print(f"STW Intelligence: http://{args.host}:{args.port}")
    print(f"Watching: {args.log.resolve()}")
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        print("Stopping STW Intelligence...")
    finally:
        server.shutdown()
        server.server_close()
        watcher.stop()
        if provider_loop is not None:
            provider_loop.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
