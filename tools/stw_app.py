#!/usr/bin/env python3
"""Run the local STW Intelligence watcher, API, and minimal dashboard."""

from __future__ import annotations

import argparse
import json
import os
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from stw_live import LogWatcher
from stw_pipeline import connect
from stw_providers import FixtureProvider, ingest_provider_rotation, match_rotation
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
    def __init__(self, database: Path, dashboard: Path = DASHBOARD):
        self.database = database.resolve()
        self.dashboard = dashboard

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
            if parsed.path == "/api/health":
                return self._json(HTTPStatus.OK, application_health(connection))
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


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=Path("data/stw-intelligence.sqlite3"))
    parser.add_argument("--log", type=Path, default=_default_log())
    parser.add_argument("--fixture", type=Path, default=DEFAULT_FIXTURE)
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

    connection = connect(args.db)
    try:
        if args.fixture and args.fixture.exists():
            ingestion = ingest_provider_rotation(
                connection, FixtureProvider(args.fixture)
            )
            match_rotation(connection, ingestion["rotation_id"])
    finally:
        connection.close()

    watcher = LogWatcher(
        args.db,
        args.log,
        poll_interval=0.5,
        start_at_end=not args.from_start,
    )
    watcher.start()
    server = ThreadingHTTPServer(
        (args.host, args.port), handler_for(ApiApplication(args.db))
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
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
