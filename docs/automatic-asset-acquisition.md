# Automatic Fortnite asset acquisition

STW Intelligence can acquire the exact local Fortnite assets required by its reference
graph without manually searching in FModel. The adapter uses CUE4Parse to mount the same
installed archives that FModel reads, exports selected packages as JSON, and feeds those
files through the existing versioned ingestion and normalization pipeline.

## One-time setup

- Install the .NET 10 SDK, or place a local SDK at `.tools/dotnet/dotnet.exe`.
- Configure Fortnite in FModel and download a mapping/AES keys as usual.
- Keep FModel's raw export directory outside this repository.

Verify and build the adapter:

```powershell
python tools/stw_asset_acquisition.py doctor
python tools/stw_asset_acquisition.py build --restore
```

The NuGet graph explicitly pins `Microsoft.Bcl.Memory` 10.0.10. Build warnings are
treated as errors so a package-audit warning cannot be silently ignored.

## Close the semantic dependency graph

Close FModel itself, then run:

```powershell
python tools/stw_asset_acquisition.py `
  --db data/phase2-real-validation.sqlite3 `
  close --max-priority 2 --max-packages 500 --max-rounds 5 --confirm-export
```

Each round performs these steps:

1. Generate a deduplicated manifest from structurally unresolved references.
2. Preview package matches and enforce the package limit.
3. Export matching packages atomically into FModel's configured export directory.
4. Ingest a new immutable snapshot and normalize it.
5. Recompute the next exact queue.

The command stops when the priority 0-2 queue is empty, unchanged, blocked, or reaches
the requested round limit. A rising raw unresolved-reference count is not itself a
failure: newly exported assets expose ordinary cosmetic, audio, UI, and engine links.
The priority queue is the semantic closure boundary.

To inspect or run a single queue manually:

```powershell
python tools/stw_asset_acquisition.py queue data/current-asset-queue.json
python tools/stw_asset_acquisition.py export data/current-asset-queue.json
python tools/stw_asset_acquisition.py export data/current-asset-queue.json --confirm-export
```

The first `export` is preview-only. The second requires explicit confirmation.

## Controlled folder discovery

Manifests use exact packages or approved STW/shared folders:

```json
{
  "schema_version": 1,
  "scopes": [
    {"kind": "package", "path": "/SaveTheWorld/Heroes/Commando/GameplayDefinition/HGD_Commando_GrenadeGun"},
    {"kind": "folder", "path": "/SaveTheWorld/GameplayEffectTemplates/Hero"}
  ]
}
```

Folder runs return package counts and immediate-child summaries before writing. Exact
packages may follow structural references into shared `/Game` content, while broad
folder exports are restricted to STW and approved shared gameplay roots.

## Safety and provenance

- Fortnite archives are opened read-only; the adapter does not alter the installation.
- Network/on-demand package downloads are disabled.
- FModel AES data is read in-process and never included in reports or the database.
- Raw exports inside the Git working tree are refused.
- FModel must be closed before writes to prevent concurrent output changes.
- An archive/mapping fingerprint must remain stable throughout every export.
- Export files are written to temporary files and atomically replaced.
- Source path, content hash, build metadata, and exporter version are preserved by the
  existing snapshot pipeline.
- Missing or failed packages remain explicit; paths and semantics are never guessed.
