from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
from stw_admin import (  # noqa: E402
    ApplicationLock,
    backup_database,
    diagnostics,
    get_settings,
    prune_history,
    restore_database,
    update_settings,
)
from stw_app import ApiApplication  # noqa: E402
from stw_pipeline import connect  # noqa: E402


class AdminTests(unittest.TestCase):
    def test_application_lock_prevents_duplicate_instances(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "app.lock"
            first = ApplicationLock(path)
            second = ApplicationLock(path)
            first.acquire()
            try:
                with self.assertRaises(RuntimeError):
                    second.acquire()
            finally:
                first.release()
            second.acquire()
            second.release()

    def test_settings_validate_persist_and_report_restart(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "stw.sqlite3"
            active_log = root / "active.log"
            active_log.touch()
            replacement = root / "replacement.log"
            connection = connect(database)
            try:
                result = update_settings(
                    connection,
                    {
                        "configured_log_path": str(replacement),
                        "history_retention_days": 90,
                        "backup_keep": 3,
                        "auto_backup_on_start": False,
                        "timezone": "America/Los_Angeles",
                    },
                    active_log,
                )
                settings = get_settings(connection, database, active_log)
                with self.assertRaises(ValueError):
                    update_settings(connection, {"backup_keep": 0})
                with self.assertRaises(ValueError):
                    update_settings(connection, {"unknown": True})
                with self.assertRaises(ValueError):
                    update_settings(connection, {"timezone": "Not/AZone"})
            finally:
                connection.close()

        self.assertTrue(result["restart_required"])
        self.assertEqual(90, settings["history_retention_days"])
        self.assertEqual(3, settings["backup_keep"])
        self.assertFalse(settings["auto_backup_on_start"])
        self.assertEqual("America/Los_Angeles", settings["timezone"])
        self.assertFalse(settings["privacy"]["raw_participant_identifiers_in_database"])

    def test_verified_backup_and_confirmed_restore(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "stw.sqlite3"
            connection = connect(database)
            with connection:
                connection.execute(
                    "INSERT INTO app_metadata(key, value) VALUES ('marker', 'before')"
                )
            connection.close()

            backup = backup_database(database, root / "backups", keep=2)
            connection = connect(database)
            with connection:
                connection.execute(
                    "UPDATE app_metadata SET value='after' WHERE key='marker'"
                )
            connection.close()
            with self.assertRaises(ValueError):
                restore_database(database, Path(backup["path"]))
            restored = restore_database(
                database, Path(backup["path"]), confirmed=True
            )
            connection = connect(database)
            try:
                marker = connection.execute(
                    "SELECT value FROM app_metadata WHERE key='marker'"
                ).fetchone()[0]
            finally:
                connection.close()

        self.assertTrue(backup["verified"])
        self.assertEqual("restored", restored["status"])
        self.assertEqual("before", marker)

    def test_retention_requires_confirmation_and_preserves_recent_history(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "stw.sqlite3"
            connection = connect(database)
            try:
                connection.execute(
                    """
                    INSERT INTO capture_files(
                        content_sha256, source_path, size_bytes, modified_ns, attempt_count
                    ) VALUES ('retention', 'fixture', 1, 1, 2)
                    """
                )
                capture = connection.execute("SELECT id FROM capture_files").fetchone()[0]
                connection.executemany(
                    """
                    INSERT INTO mission_attempts(
                        capture_id, source_attempt_index, source_line_start,
                        started_at, outcome
                    ) VALUES (?, ?, ?, ?, 'joined')
                    """,
                    [
                        (capture, 0, 1, "2026-01-01T00:00:00Z"),
                        (capture, 1, 2, "2026-08-07T00:00:00Z"),
                    ],
                )
                connection.commit()
                with self.assertRaises(ValueError):
                    prune_history(connection, 30)
                result = prune_history(
                    connection,
                    30,
                    datetime(2026, 8, 8, tzinfo=timezone.utc),
                    confirmed=True,
                )
                remaining = connection.execute(
                    "SELECT started_at FROM mission_attempts"
                ).fetchall()
            finally:
                connection.close()

        self.assertEqual(1, result["deleted"])
        self.assertEqual(["2026-08-07T00:00:00Z"], [row[0] for row in remaining])

    def test_diagnostics_and_admin_api(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "stw.sqlite3"
            log = root / "FortniteGame.log"
            log.touch()
            connection = connect(database)
            try:
                report = diagnostics(connection, database, log, ROOT / "web" / "index.html")
            finally:
                connection.close()
            api = ApiApplication(database, active_log=log)
            settings_status, _, settings_body = api.dispatch("GET", "/api/settings")
            save_status, _, save_body = api.dispatch(
                "POST",
                "/api/settings",
                json.dumps({"backup_keep": 2}).encode(),
            )
            invalid_status, _, _ = api.dispatch(
                "POST", "/api/settings", b'{"history_retention_days":-1}'
            )
            backup_status, _, backup_body = api.dispatch(
                "POST", "/api/admin/backup", b"{}"
            )
            diagnostic_status, _, diagnostic_body = api.dispatch(
                "GET", "/api/diagnostics"
            )

        self.assertIn(report["status"], ("pass", "warn"))
        self.assertEqual(200, settings_status)
        self.assertEqual(200, save_status)
        self.assertEqual(2, json.loads(save_body)["settings"]["backup_keep"])
        self.assertEqual(400, invalid_status)
        self.assertEqual(200, backup_status)
        self.assertTrue(json.loads(backup_body)["verified"])
        self.assertEqual(200, diagnostic_status)
        self.assertIn("checks", json.loads(diagnostic_body))
        self.assertIn("privacy", json.loads(settings_body))


if __name__ == "__main__":
    unittest.main()
