#!/usr/bin/env python3
"""Checkpointed live Fortnite log watching and state reconstruction."""

from __future__ import annotations

import hashlib
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from analyze_telemetry import analyze
from stw_pipeline import (
    _privacy_salt,
    connect,
    ensure_live_capture,
    persist_live_analysis,
    persist_live_state_events,
)
from stw_providers import latest_rotation_id, match_rotation


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _file_identity(path: Path) -> str:
    stat = path.stat()
    inode = getattr(stat, "st_ino", 0)
    if inode:
        return f"{stat.st_dev}:{inode}"
    return f"created:{stat.st_ctime_ns}"


def _tail_bytes(path: Path, offset: int, length: int = 128) -> bytes:
    if offset <= 0:
        return b""
    count = min(offset, length)
    with path.open("rb") as source:
        source.seek(offset - count)
        return source.read(count)


class LogWatcher:
    def __init__(
        self,
        database: Path,
        source: Path,
        state_directory: Path | None = None,
        poll_interval: float = 0.5,
        start_at_end: bool = True,
    ) -> None:
        self.database = database.resolve()
        self.source = source.resolve()
        self.state_directory = (
            state_directory.resolve()
            if state_directory
            else (self.database.parent / "live").resolve()
        )
        self.poll_interval = poll_interval
        self.start_at_end = start_at_end
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.state_directory.mkdir(parents=True, exist_ok=True)
        self.watcher_id = self._initialize()

    def _initialize(self) -> int:
        connection = connect(self.database)
        try:
            row = connection.execute(
                "SELECT * FROM log_watchers WHERE source_path=?", (str(self.source),)
            ).fetchone()
            if row is not None:
                spool = Path(row["spool_path"])
                if not spool.exists() and row["spool_size"]:
                    self._begin_generation(connection, row, None, "spool_missing")
                else:
                    spool.parent.mkdir(parents=True, exist_ok=True)
                    spool.touch(exist_ok=True)
                    if spool.stat().st_size > row["spool_size"]:
                        with spool.open("r+b") as output:
                            output.truncate(row["spool_size"])
                return row["id"]
            exists = self.source.exists()
            identity = _file_identity(self.source) if exists else None
            offset = self.source.stat().st_size if exists and self.start_at_end else 0
            tail = _tail_bytes(self.source, offset) if exists else b""
            seed = hashlib.sha256(str(self.source).encode("utf-8")).hexdigest()[:16]
            spool = self.state_directory / f"watch-{seed}-g0.log"
            spool.touch(exist_ok=True)
            with connection:
                cursor = connection.execute(
                    """
                    INSERT INTO log_watchers(
                        source_path, file_identity, generation, byte_offset,
                        partial_bytes, tail_bytes, spool_path, spool_size, status
                    ) VALUES (?, ?, 0, ?, X'', ?, ?, 0, ?)
                    """,
                    (
                        str(self.source),
                        identity,
                        offset,
                        tail,
                        str(spool),
                        "watching" if exists else "missing",
                    ),
                )
                watcher_id = cursor.lastrowid
                connection.execute(
                    """
                    INSERT INTO live_watch_generations(
                        watcher_id, generation, file_identity, spool_path
                    ) VALUES (?, 0, ?, ?)
                    """,
                    (watcher_id, identity, str(spool)),
                )
                connection.execute(
                    """
                    INSERT INTO live_states(watcher_id, generation, state, reason)
                    VALUES (?, 0, 'Idle', 'watcher_started')
                    """,
                    (watcher_id,),
                )
            return watcher_id
        finally:
            connection.close()

    def _begin_generation(
        self,
        connection: sqlite3.Connection,
        row: sqlite3.Row,
        identity: str | None,
        reason: str,
    ) -> sqlite3.Row:
        generation = row["generation"] + 1
        seed = hashlib.sha256(str(self.source).encode("utf-8")).hexdigest()[:16]
        spool = self.state_directory / f"watch-{seed}-g{generation}.log"
        spool.touch(exist_ok=True)
        with connection:
            connection.execute(
                """
                UPDATE live_watch_generations SET ended_at=CURRENT_TIMESTAMP
                WHERE watcher_id=? AND generation=?
                """,
                (row["id"], row["generation"]),
            )
            connection.execute(
                """
                INSERT INTO live_watch_generations(
                    watcher_id, generation, file_identity, spool_path
                ) VALUES (?, ?, ?, ?)
                """,
                (row["id"], generation, identity, str(spool)),
            )
            connection.execute(
                """
                UPDATE log_watchers SET
                    file_identity=?, generation=?, byte_offset=0, partial_bytes=X'', tail_bytes=X'',
                    spool_path=?, spool_size=0, status='watching',
                    checkpoint_at=CURRENT_TIMESTAMP, last_error=NULL
                WHERE id=?
                """,
                (identity, generation, str(spool), row["id"]),
            )
            connection.execute(
                """
                INSERT INTO live_states(watcher_id, generation, state, reason)
                VALUES (?, ?, 'Idle', ?)
                ON CONFLICT(watcher_id) DO UPDATE SET
                    generation=excluded.generation,
                    state='Idle', attempt_id=NULL, occurred_at=NULL,
                    source_line=NULL, reason=excluded.reason,
                    updated_at=CURRENT_TIMESTAMP
                """,
                (row["id"], generation, reason),
            )
        return connection.execute(
            "SELECT * FROM log_watchers WHERE id=?", (row["id"],)
        ).fetchone()

    def poll_once(self) -> dict[str, Any]:
        connection = connect(self.database)
        try:
            row = connection.execute(
                "SELECT * FROM log_watchers WHERE id=?", (self.watcher_id,)
            ).fetchone()
            if not self.source.exists():
                with connection:
                    connection.execute(
                        """
                        UPDATE log_watchers SET status='missing', checkpoint_at=CURRENT_TIMESTAMP,
                            last_error='source log does not exist'
                        WHERE id=?
                        """,
                        (self.watcher_id,),
                    )
                return {"bytes": 0, "lines": 0, "status": "missing"}
            identity = _file_identity(self.source)
            size = self.source.stat().st_size
            tail_changed = False
            if row["tail_bytes"] and size >= row["byte_offset"]:
                tail_changed = _tail_bytes(
                    self.source, row["byte_offset"], len(row["tail_bytes"])
                ) != bytes(row["tail_bytes"])
            if row["file_identity"] is not None and (
                identity != row["file_identity"]
                or size < row["byte_offset"]
                or tail_changed
            ):
                reason = (
                    "file_rotated"
                    if identity != row["file_identity"]
                    else "file_truncated_or_rewritten"
                )
                row = self._begin_generation(connection, row, identity, reason)
            elif row["file_identity"] is None:
                with connection:
                    connection.execute(
                        "UPDATE log_watchers SET file_identity=?, status='watching' WHERE id=?",
                        (identity, self.watcher_id),
                    )
                row = connection.execute(
                    "SELECT * FROM log_watchers WHERE id=?", (self.watcher_id,)
                ).fetchone()
            spool = Path(row["spool_path"])
            spool.parent.mkdir(parents=True, exist_ok=True)
            spool.touch(exist_ok=True)
            if spool.stat().st_size > row["spool_size"]:
                with spool.open("r+b") as output:
                    output.truncate(row["spool_size"])
            if size <= row["byte_offset"]:
                with connection:
                    connection.execute(
                        """
                        UPDATE log_watchers SET status='watching', checkpoint_at=CURRENT_TIMESTAMP,
                            last_error=NULL WHERE id=?
                        """,
                        (self.watcher_id,),
                    )
                return {"bytes": 0, "lines": 0, "status": "watching"}
            with self.source.open("rb") as source:
                source.seek(row["byte_offset"])
                data = source.read()
            combined = bytes(row["partial_bytes"]) + data
            newline = combined.rfind(b"\n")
            complete = combined[: newline + 1] if newline >= 0 else b""
            partial = combined[newline + 1 :] if newline >= 0 else combined
            if complete:
                with spool.open("ab") as output:
                    output.write(complete)
                    output.flush()
                result = analyze(spool, privacy_salt=_privacy_salt(connection))
                capture_id = ensure_live_capture(
                    connection,
                    self.watcher_id,
                    row["generation"],
                    self.source,
                    spool,
                    identity,
                )
                persistence = persist_live_analysis(
                    connection, capture_id, self.source, spool, result
                )
                state_events = persist_live_state_events(
                    connection,
                    self.watcher_id,
                    row["generation"],
                    capture_id,
                    result,
                )
                rotation_id = latest_rotation_id(connection)
                if rotation_id is not None:
                    match_rotation(connection, rotation_id)
            else:
                persistence = {}
                state_events = 0
            event_at = _utc_now() if complete else row["last_event_at"]
            with connection:
                connection.execute(
                    """
                    UPDATE log_watchers SET
                        byte_offset=?, partial_bytes=?, tail_bytes=?, spool_size=?, status='watching',
                        checkpoint_at=CURRENT_TIMESTAMP, last_event_at=?, last_error=NULL
                    WHERE id=?
                    """,
                    (
                        row["byte_offset"] + len(data),
                        partial,
                        _tail_bytes(self.source, row["byte_offset"] + len(data)),
                        spool.stat().st_size,
                        event_at,
                        self.watcher_id,
                    ),
                )
            return {
                "bytes": len(data),
                "lines": complete.count(b"\n"),
                "state_events": state_events,
                "persistence": persistence,
                "status": "watching",
            }
        except Exception as error:
            with connection:
                connection.execute(
                    """
                    UPDATE log_watchers SET status='error', last_error=?,
                        checkpoint_at=CURRENT_TIMESTAMP WHERE id=?
                    """,
                    (str(error), self.watcher_id),
                )
            raise
        finally:
            connection.close()

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="stw-log-watcher", daemon=True
        )
        self._thread.start()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.poll_once()
            except Exception:
                pass
            self._stop.wait(self.poll_interval)

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=max(2.0, self.poll_interval * 4))
        connection = connect(self.database)
        try:
            with connection:
                connection.execute(
                    "UPDATE log_watchers SET status='stopped' WHERE id=?",
                    (self.watcher_id,),
                )
        finally:
            connection.close()


def watcher_health(connection: sqlite3.Connection) -> list[dict[str, Any]]:
    return [
        dict(row)
        for row in connection.execute(
            """
            SELECT id, source_path, generation, byte_offset, status, checkpoint_at,
                   last_event_at, last_error
            FROM log_watchers ORDER BY id
            """
        )
    ]
