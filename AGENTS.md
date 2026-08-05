# AGENTS.md

Instructions for AI coding agents (GitHub Copilot, Claude Code, Cursor, etc.) working in this repository.

## What this project is

A local-only dashboard that archives and visualizes **GitHub Copilot AI Credits (AIC)** consumption from `~/.copilot/session-store.db`, at hourly granularity. Python standard library + PowerShell + one static HTML file. **No third-party dependencies. Do not add any.**

```
~/.copilot/session-store.db ──(read-only)──▶ archive.db ──▶ data/usage.js ──▶ index.html
```

## Non-negotiable invariants

Violating any of these is a correctness/data-loss bug, not a style preference.

1. **Never write to the live Copilot DB.** Open it only as `file:...?mode=ro` with `PRAGMA query_only=ON`. Do not add migrations, `VACUUM`, or any statement that touches it.
2. **Never copy the `db` / `-wal` / `-shm` trio of a live database.** Sequential copies can interleave with a checkpoint and produce an inconsistent snapshot. If the DB is locked, retry with backoff and then skip ingestion. All live-DB reads go through `aic_archive.connect_readonly()` — do not add a second path. `verify_pricing.py` deliberately reads the **archive**, not the live DB.
3. **The archive is append-only.** No `DELETE` or `DROP` on `usage_events`, `sessions`, `collect_runs`, or `quarantine_events`. Schema changes must rename the old table aside (see `migrate()`), never drop it.
4. **Read-only commands must not create the archive.** `--stats` / `--backup-to` / `--export-csv` fail when the archive is absent. A backup that silently succeeds against a freshly created empty DB is worse than an error.
5. **Do not change the archive primary key** `(session_id, id, created_at)` without reading §4.1 of `README.ja.md`. `id` is `AUTOINCREMENT` and restarts at 1 when the DB is recreated; weaker keys silently destroy history.
6. **No network calls.** Nothing in this repo may contact a remote host at runtime.
7. **Never commit real usage data.** `data/`, `export/`, `sample/`, `*.db`, `*.csv`, `docs/*.png`, and `config.local.json` are gitignored. Screenshots and demo output must come from `tools/make_sample_db.py`, never from a real archive.
8. **Missing data is not zero.** Buckets with no coverage must render as shaded bands, be excluded from moving averages and `avg_daily_7d`, and break the MA polyline rather than dropping it to zero. Do not "simplify" this into zero-valued bars.
9. **Do not overstate gap detection.** Timestamps alone cannot prove deletion. Keep the `high` / `low` confidence distinction and the hedged wording. `source_identity()` must stay size-independent, and must return `None` when the filesystem cannot supply a stable inode — an unknown generation may never be promoted to `high`.
10. **Distinguish "locked" from "schema changed".** Only BUSY/LOCKED errors are retried; anything else raises `UnsupportedSchema` and is logged as such. Telling a user to "wait and retry" when Copilot changed its internal schema sends them down the wrong path.
11. **Do not claim coverage the data cannot support.** Copilot Coding Agent, Code Review, VS Code Chat, and other machines leave **no** local usage rows — the cloud session store has no `total_nano_aiu` at all and uses a disjoint session ID space. Never present the dashboard total as total Copilot spend, and never add a "sync from cloud" path on the assumption that AIC is retrievable there.
12. **`--reconcile` must fail loud in only one direction.** Archive **>** app total is normal (sub-agent and compaction events are counted individually). Only *missing* sessions and *short* sessions indicate data loss. Sessions still in flight at the last collection must be excluded via the `in_flight` check, or every run reports a false positive. A missing or schema-mismatched `data.db` exits **0** with `[skip]` — never a silent success implying verification happened.

## Layout

| Path | Role |
| --- | --- |
| `aic_archive.py` | Archive: schema, migration, merge, gap detection, backup, CSV, reconciliation, process lock |
| `aic_collect.py` | Aggregation into `data/usage.json` + `data/usage.js` |
| `index.html` | Dashboard. Two inline `<script>` blocks: theme bootstrap in `<head>`, app at the bottom |
| `verify_pricing.py` | Re-derives AIC from token counts and official prices |
| `run-dashboard.ps1` | Day-to-day driver and scheduled-task management |
| `setup.ps1` | First-time setup |
| `tools/make_sample_db.py` | Synthetic data generator |

## Conventions

- **Python:** 3.9+ compatible, standard library only, 4-space indent, type hints where they clarify. Comments explain *why*, not *what*. Japanese comments are the norm in `aic_*.py`.
- **JavaScript:** no framework, no bundler. `index.html` must keep working from `file://` — that is why data is loaded via `<script src="data/usage.js">` (which assigns `window.AIC_DATA`) rather than `fetch()`.
- **PowerShell:** use `$PSScriptRoot`, not `Split-Path -LiteralPath ... -Parent` (parameter-set ambiguity). Avoid `[CmdletBinding(DefaultParameterSetName=...)]` with multiple sets; use plain switches plus `switch ($true)`. Check `$LASTEXITCODE` after every Python invocation. Write files with `[System.IO.File]::WriteAllText(..., UTF8Encoding($false))` — `Set-Content -Encoding UTF8` emits a BOM on PowerShell 5.1 that Python's `json.loads` rejects.
- **Config:** `config.json` is tracked and shared. Machine-specific values belong in `config.local.json` (gitignored). Never write user paths into `config.json`.
- **Output files** must be written to a temp file in the same directory and swapped in with `os.replace()`.
- **Docs:** `README.md` (English) and `README.ja.md` (Japanese) are both first-class. Any user-facing change must update **both**.

## Verifying a change

```powershell
# 1. Idempotency — a second run must report 新規 0 / 変更 0
python aic_archive.py ; python aic_archive.py

# 2. Full pipeline
python aic_collect.py

# 3. Front-end syntax (both inline script blocks)
#    extract each <script> without src= and run: node --check

# 4. End-to-end on synthetic data only
.\run-dashboard.ps1 -Demo -NoOpen
```

For UI work, verify with a headless browser and assert **zero console errors**, plus the expected counts of `.kpi`, `.banner`, `rect.gap-band`, and heatmap cells.

To exercise gap handling, inject synthetic `incomplete` flags and `meta.gaps` into a copy of `usage.json`. Never fabricate rows inside a real archive.

## Things that have already bitten us

- `Split-Path -LiteralPath $x -Parent` throws `AmbiguousParameterSet`, and the error points at the caller's line rather than the real cause.
- PowerShell output is cp932 by default; set `$env:PYTHONIOENCODING='utf-8'` before invoking Python.
- `Set-Content -Encoding UTF8` writes a BOM on PowerShell 5.1, which breaks Python JSON parsing.
- `Get-Command python` can return the Microsoft Store alias in `WindowsApps` even when Python is not installed. Always execute the candidate and validate its reported version before selecting it.
- Playwright cannot call `inner_text()` on SVG `<text>`; use `text_content()`.
- `CREATE TABLE IF NOT EXISTS` will not add columns to a table created by an older version — use the `_ensure_columns()` helper.
- `New-ScheduledTaskTrigger -AtLogOn` has no repetition parameter. Register two triggers (`-AtLogOn` and `-Once ... -RepetitionInterval`) rather than grafting `.Repetition` onto the CIM object, and omit `RepetitionDuration` to mean "indefinitely".
- `Register-ScheduledTask` goes through the CIM provider and is blocked by policy on many managed corporate devices (`Access is denied`) even for the user's own tasks. `schtasks.exe` uses a different path and usually succeeds unelevated — keep it as the fallback, and make `-UninstallTask` handle tasks created either way.
- A `<polyline>` with `null` values coerced to `0` reads as a real drop to zero. Break the line into segments instead.
- `connect_readonly()` originally hardcoded its probe against `assistant_usage_events`, so pointing it at `~/.copilot/data.db` raised `UnsupportedSchema`. It takes a `probe` table name now — pass the one the caller actually needs.
- Exit codes are load-bearing: `1` = error, `2` = corrupt DB, `3` = reconciliation found signs of loss. `run-dashboard.ps1` must map `3` to `Write-Warning`, not `throw`.

## Out of scope

Do not add: telemetry, cloud sync, a web server, auth, a package manager, or any dependency. If a feature seems to require one, raise it instead of implementing it.
