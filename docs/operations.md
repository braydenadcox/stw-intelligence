# STW Intelligence operations

## Start on Windows

Double-click `start-stw.cmd`. It starts the local application and opens
<http://127.0.0.1:8765>. Close it with `Ctrl+C` in the application window.

The Settings page shows the active Fortnite log and database paths, privacy behavior,
retention controls, backup controls, watcher health, provider health, and database
integrity. A changed log path takes effect after restarting the application.

## Diagnostics

Run read-only checks when the dashboard reports a problem:

```powershell
python tools/stw_admin.py diagnostics
```

The report checks SQLite integrity and writability, the configured log, watcher state,
dashboard asset, and telemetry history. The local API also exposes `/api/diagnostics`.

## Backups

By default, startup creates a verified SQLite backup and retains the newest seven in
`data/backups/`. Change this behavior from Settings. Create one manually with:

```powershell
python tools/stw_admin.py backup
```

Backups and the live database are local-only and ignored by Git.

## Restore

Stop STW Intelligence before restoring. The restore command verifies the selected
backup, creates a safety backup of the current database, and then replaces it. The
`--yes` flag is mandatory so a restore cannot happen accidentally.

```powershell
python tools/stw_admin.py restore "data\backups\stw-intelligence-TIMESTAMP.sqlite3" --yes
```

Restart the application and run diagnostics after recovery.

## Privacy and retention

Raw participant identifiers are never written to normalized database rows. The live
watcher sanitizes complete log lines before placing them in its local parsing spool, and
database relationships use a private salted HMAC pseudonym.

History is retained forever by default. To opt into cleanup, set a number of days in
Settings, save, create a backup, and select **Apply retention now**. Cleanup requires an
explicit confirmation and never deletes the active attempt. The same operation is
available from the CLI:

```powershell
python tools/stw_admin.py prune --days 90 --yes
```
