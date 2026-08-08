#!/usr/bin/env python3
"""Administration, diagnostics, backup, recovery, and retention for STW Intelligence."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from sanitize_logs import sanitize_file


SETTING_PREFIX = "setting."
DEFAULTS: dict[str, Any] = {
    "configured_log_path": None,
    "history_retention_days": 0,
    "backup_keep": 7,
    "auto_backup_on_start": True,
}


class ApplicationLock:
    """A process-held lock preventing two hardened app instances sharing one database."""

    def __init__(self, path: Path):
        self.path = path.resolve()
        self._file = None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self._file = self.path.open("a+b")
            self._file.seek(0)
            if self._file.read(1) == b"":
                self._file.write(b"0")
                self._file.flush()
            self._file.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self._file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            if self._file is not None:
                self._file.close()
            self._file = None
            raise RuntimeError("STW Intelligence is already running") from error

    def release(self) -> None:
        if self._file is None:
            return
        try:
            self._file.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self._file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._file.fileno(), fcntl.LOCK_UN)
        finally:
            self._file.close()
            self._file = None


def _utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")


def _parse_observed_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        if "." in value[:10]:
            return datetime.strptime(value, "%Y.%m.%d-%H.%M.%S:%f").replace(
                tzinfo=timezone.utc
            )
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def get_settings(
    connection: sqlite3.Connection,
    database: Path | None = None,
    active_log: Path | None = None,
) -> dict[str, Any]:
    settings = dict(DEFAULTS)
    rows = connection.execute(
        "SELECT key, value FROM app_metadata WHERE key LIKE 'setting.%'"
    ).fetchall()
    for row in rows:
        name = row["key"][len(SETTING_PREFIX) :]
        if name in settings:
            try:
                settings[name] = json.loads(row["value"])
            except json.JSONDecodeError:
                pass
    settings["database_path"] = str(database.resolve()) if database else None
    settings["active_log_path"] = str(active_log.resolve()) if active_log else None
    settings["privacy"] = {
        "storage_scope": "local_only",
        "participant_identifiers": "salted_hmac_pseudonyms",
        "raw_participant_identifiers_in_database": False,
        "live_spool_sanitization": True,
    }
    return settings


def update_settings(
    connection: sqlite3.Connection, changes: dict[str, Any], active_log: Path | None = None
) -> dict[str, Any]:
    allowed = set(DEFAULTS)
    unknown = set(changes) - allowed
    if unknown:
        raise ValueError(f"unsupported setting(s): {', '.join(sorted(unknown))}")
    normalized: dict[str, Any] = {}
    if "configured_log_path" in changes:
        value = changes["configured_log_path"]
        if value in (None, ""):
            normalized["configured_log_path"] = None
        elif not isinstance(value, str):
            raise ValueError("configured_log_path must be a path string")
        else:
            normalized["configured_log_path"] = str(Path(value).expanduser().resolve())
    for key, minimum, maximum in (
        ("history_retention_days", 0, 3650),
        ("backup_keep", 1, 50),
    ):
        if key in changes:
            value = changes[key]
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValueError(f"{key} must be an integer")
            if not minimum <= value <= maximum:
                raise ValueError(f"{key} must be between {minimum} and {maximum}")
            normalized[key] = value
    if "auto_backup_on_start" in changes:
        value = changes["auto_backup_on_start"]
        if not isinstance(value, bool):
            raise ValueError("auto_backup_on_start must be true or false")
        normalized["auto_backup_on_start"] = value
    with connection:
        connection.executemany(
            """
            INSERT INTO app_metadata(key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value
            """,
            [
                (SETTING_PREFIX + key, json.dumps(value))
                for key, value in normalized.items()
            ],
        )
    restart_required = False
    if "configured_log_path" in normalized:
        restart_required = normalized["configured_log_path"] != (
            str(active_log.resolve()) if active_log else None
        )
    return {"updated": sorted(normalized), "restart_required": restart_required}


def preflight_settings(database: Path) -> dict[str, Any]:
    settings = dict(DEFAULTS)
    if not database.exists() or database.stat().st_size == 0:
        return settings
    try:
        connection = sqlite3.connect(database)
        try:
            exists = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='app_metadata'"
            ).fetchone()
            if not exists:
                return settings
            for key, value in connection.execute(
                "SELECT key, value FROM app_metadata WHERE key LIKE 'setting.%'"
            ):
                name = key[len(SETTING_PREFIX) :]
                if name in settings:
                    settings[name] = json.loads(value)
        finally:
            connection.close()
    except (sqlite3.Error, json.JSONDecodeError, OSError):
        pass
    return settings


def backup_database(
    database: Path, backup_directory: Path | None = None, keep: int = 7
) -> dict[str, Any]:
    database = database.resolve()
    if not database.exists() or database.stat().st_size == 0:
        return {"status": "skipped", "reason": "database does not exist yet"}
    backup_directory = (backup_directory or database.parent / "backups").resolve()
    backup_directory.mkdir(parents=True, exist_ok=True)
    destination = backup_directory / f"{database.stem}-{_utc_stamp()}.sqlite3"
    partial = destination.with_suffix(".sqlite3.partial")
    source = sqlite3.connect(database)
    target = sqlite3.connect(partial)
    try:
        source.backup(target)
        check = target.execute("PRAGMA integrity_check").fetchone()[0]
        if check != "ok":
            raise sqlite3.DatabaseError(f"backup integrity check failed: {check}")
    finally:
        target.close()
        source.close()
    os.replace(partial, destination)
    backups = sorted(
        backup_directory.glob(f"{database.stem}-*.sqlite3"),
        key=lambda path: path.stat().st_mtime_ns,
        reverse=True,
    )
    removed = []
    for old in backups[max(1, keep) :]:
        old.unlink()
        removed.append(str(old))
    return {
        "status": "created",
        "path": str(destination),
        "size_bytes": destination.stat().st_size,
        "verified": True,
        "removed_old_backups": removed,
    }


def restore_database(
    database: Path, backup: Path, *, confirmed: bool = False
) -> dict[str, Any]:
    if not confirmed:
        raise ValueError("restore requires explicit confirmation")
    database = database.resolve()
    backup = backup.resolve()
    if not backup.is_file():
        raise FileNotFoundError(backup)
    source = sqlite3.connect(backup)
    try:
        check = source.execute("PRAGMA integrity_check").fetchone()[0]
        if check != "ok":
            raise sqlite3.DatabaseError(f"backup integrity check failed: {check}")
        safety = backup_database(database, database.parent / "backups", keep=50)
        temporary = database.with_suffix(database.suffix + ".restore-partial")
        target = sqlite3.connect(temporary)
        try:
            source.backup(target)
        finally:
            target.close()
        os.replace(temporary, database)
        for suffix in ("-wal", "-shm"):
            sidecar = Path(str(database) + suffix)
            if sidecar.exists():
                sidecar.unlink()
    finally:
        source.close()
    return {"status": "restored", "backup": str(backup), "safety_backup": safety}


def prune_history(
    connection: sqlite3.Connection,
    retention_days: int,
    now: datetime | None = None,
    *,
    confirmed: bool = False,
) -> dict[str, Any]:
    if not confirmed:
        raise ValueError("retention cleanup requires explicit confirmation")
    if retention_days <= 0:
        return {"status": "skipped", "reason": "history retention is forever", "deleted": 0}
    instant = now or datetime.now(timezone.utc)
    if instant.tzinfo is None:
        instant = instant.replace(tzinfo=timezone.utc)
    cutoff = instant - timedelta(days=retention_days)
    active_ids = {
        row[0]
        for row in connection.execute(
            "SELECT attempt_id FROM live_states WHERE attempt_id IS NOT NULL"
        )
    }
    old_ids = [
        row["id"]
        for row in connection.execute("SELECT id, started_at FROM mission_attempts")
        if row["id"] not in active_ids
        and (observed := _parse_observed_time(row["started_at"])) is not None
        and observed < cutoff
    ]
    if not old_ids:
        return {"status": "complete", "cutoff": cutoff.isoformat(), "deleted": 0}
    placeholders = ",".join("?" for _ in old_ids)
    with connection:
        for table in (
            "attempt_activity_scores",
            "membership_events",
            "attempt_maps",
            "assignments",
            "live_state_events",
        ):
            connection.execute(
                f"DELETE FROM {table} WHERE attempt_id IN ({placeholders})", old_ids
            )
        connection.execute(
            f"DELETE FROM mission_attempts WHERE id IN ({placeholders})", old_ids
        )
        connection.execute("DELETE FROM regional_activity")
    return {
        "status": "complete",
        "cutoff": cutoff.isoformat(),
        "deleted": len(old_ids),
    }


def sanitize_live_spools(connection: sqlite3.Connection) -> dict[str, int]:
    sanitized = 0
    missing = 0
    active = 0
    for row in connection.execute(
        """
        SELECT g.watcher_id, g.generation, g.spool_path,
               w.generation AS current_generation, w.status AS watcher_status
        FROM live_watch_generations g
        JOIN log_watchers w ON w.id=g.watcher_id
        """
    ).fetchall():
        if (
            row["generation"] == row["current_generation"]
            and row["watcher_status"] == "watching"
        ):
            active += 1
            continue
        path = Path(row["spool_path"])
        if not path.is_file():
            missing += 1
            continue
        sanitize_file(path)
        sanitized += 1
        connection.execute(
            """
            UPDATE log_watchers SET spool_size=?
            WHERE id=? AND generation=?
            """,
            (path.stat().st_size, row["watcher_id"], row["generation"]),
        )
    connection.commit()
    return {"sanitized": sanitized, "missing": missing, "active_skipped": active}


def diagnostics(
    connection: sqlite3.Connection,
    database: Path,
    log_path: Path | None = None,
    dashboard: Path | None = None,
    provider_runtime: dict[str, Any] | None = None,
) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []

    def add(name: str, status: str, detail: str) -> None:
        checks.append({"name": name, "status": status, "detail": detail})

    try:
        integrity = connection.execute("PRAGMA quick_check").fetchone()[0]
        add("database_integrity", "pass" if integrity == "ok" else "fail", integrity)
    except sqlite3.Error as error:
        add("database_integrity", "fail", str(error))
    version = connection.execute("PRAGMA user_version").fetchone()[0]
    add("database_schema", "pass", f"schema version {version}")
    add(
        "database_writable",
        "pass" if os.access(database.parent, os.W_OK) else "fail",
        str(database.resolve()),
    )
    if log_path is None:
        add("fortnite_log", "fail", "no log path configured")
    elif not log_path.exists():
        add("fortnite_log", "fail", f"not found: {log_path.resolve()}")
    else:
        add(
            "fortnite_log",
            "pass" if os.access(log_path, os.R_OK) else "fail",
            f"{log_path.resolve()} ({log_path.stat().st_size} bytes)",
        )
    if dashboard is not None:
        add(
            "dashboard",
            "pass" if dashboard.is_file() else "fail",
            str(dashboard.resolve()),
        )
    watchers = connection.execute(
        "SELECT status, last_error FROM log_watchers ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if watchers:
        status = (
            "pass"
            if watchers["status"] == "watching"
            else "warn"
            if watchers["status"] == "stopped"
            else "fail"
        )
        add("watcher", status, watchers["last_error"] or watchers["status"])
    else:
        add("watcher", "warn", "watcher has not started yet")
    if provider_runtime:
        provider_state = provider_runtime.get("status", "unknown")
        add(
            "provider",
            "pass" if provider_state == "healthy" else "warn",
            str(provider_runtime.get("detail") or provider_state),
        )
    attempt_count = connection.execute(
        "SELECT COUNT(*) FROM mission_attempts"
    ).fetchone()[0]
    add("telemetry_history", "pass" if attempt_count else "warn", f"{attempt_count} attempts")
    failures = sum(check["status"] == "fail" for check in checks)
    warnings = sum(check["status"] == "warn" for check in checks)
    return {
        "status": "fail" if failures else "warn" if warnings else "pass",
        "checks": checks,
        "failures": failures,
        "warnings": warnings,
        "settings": get_settings(connection, database, log_path),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=Path("data/stw-intelligence.sqlite3"))
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("backup")
    restore = commands.add_parser("restore")
    restore.add_argument("backup", type=Path)
    restore.add_argument("--yes", action="store_true")
    commands.add_parser("diagnostics")
    prune = commands.add_parser("prune")
    prune.add_argument("--days", type=int, required=True)
    prune.add_argument("--yes", action="store_true")
    args = parser.parse_args()

    if args.command == "backup":
        print(json.dumps(backup_database(args.db), indent=2))
        return 0
    if args.command == "restore":
        print(json.dumps(restore_database(args.db, args.backup, confirmed=args.yes), indent=2))
        return 0

    from stw_pipeline import connect

    connection = connect(args.db)
    try:
        if args.command == "diagnostics":
            settings = get_settings(connection)
            configured = settings["configured_log_path"]
            default_log = None
            if local_data := os.environ.get("LOCALAPPDATA"):
                default_log = (
                    Path(local_data)
                    / "FortniteGame"
                    / "Saved"
                    / "Logs"
                    / "FortniteGame.log"
                )
            print(
                json.dumps(
                    diagnostics(
                        connection,
                        args.db,
                        Path(configured) if configured else default_log,
                    ),
                    indent=2,
                )
            )
        else:
            print(
                json.dumps(
                    prune_history(connection, args.days, confirmed=args.yes), indent=2
                )
            )
        return 0
    finally:
        connection.close()


if __name__ == "__main__":
    raise SystemExit(main())
