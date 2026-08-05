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
2. **Never copy the `db` / `-wal` / `-shm` trio of a live database.** Sequential copies can interleave with a checkpoint and produce an inconsistent snapshot. If the DB is locked, retry with backoff and then skip ingestion.
3. **The archive is append-only.** No `DELETE` or `DROP` on `usage_events`, `sessions`, `collect_runs`, or `quarantine_events`. Schema changes must rename the old table aside (see `migrate()`), never drop it.
4. **Do not change the archive primary key** `(session_id, id, created_at)` without reading §4.1 of `README.ja.md`. `id` is `AUTOINCREMENT` and restarts at 1 when the DB is recreated; weaker keys silently destroy history.
5. **No network calls.** Nothing in this repo may contact a remote host at runtime.
6. **Never commit real usage data.** `data/`, `export/`, `sample/`, and `*.db` are gitignored. Screenshots and demo output must come from `tools/make_sample_db.py`, never from a real archive.
7. **Missing data is not zero.** Buckets with no coverage must render as shaded bands and be excluded from averages. Do not "simplify" this into zero-valued bars.
8. **Do not overstate gap detection.** Timestamps alone cannot prove deletion. Keep the `high` / `low` confidence distinction and the hedged wording.

## Layout

| Path | Role |
| --- | --- |
| `aic_archive.py` | Archive: schema, migration, merge, gap detection, backup, CSV, process lock |
| `aic_collect.py` | Aggregation into `data/usage.json` + `data/usage.js` |
| `index.html` | Dashboard. Two inline `<script>` blocks: theme bootstrap in `<head>`, app at the bottom |
| `verify_pricing.py` | Re-derives AIC from token counts and official prices |
| `run-dashboard.ps1` | Day-to-day driver and scheduled-task management |
| `setup.ps1` | First-time setup |
| `tools/make_sample_db.py` | Synthetic data generator |

## Conventions

- **Python:** 3.9+ compatible, standard library only, 4-space indent, type hints where they clarify. Comments explain *why*, not *what*. Japanese comments are the norm in `aic_*.py`.
- **JavaScript:** no framework, no bundler. `index.html` must keep working from `file://` — that is why data is loaded via `<script src="data/usage.js">` rather than `fetch()`.
- **PowerShell:** use `$PSScriptRoot`, not `Split-Path -LiteralPath ... -Parent` (parameter-set ambiguity). Avoid `[CmdletBinding(DefaultParameterSetName=...)]` with multiple sets; use plain switches plus `switch ($true)`.
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
- Playwright cannot call `inner_text()` on SVG `<text>`; use `text_content()`.
- `CREATE TABLE IF NOT EXISTS` will not add columns to a table created by an older version — use the `_ensure_columns()` helper.
- `New-ScheduledTaskTrigger -AtLogOn` has no repetition parameter. Register two triggers (`-AtLogOn` and `-Once ... -RepetitionInterval`) rather than grafting `.Repetition` onto the CIM object, and omit `RepetitionDuration` to mean "indefinitely".

## Out of scope

Do not add: telemetry, cloud sync, a web server, auth, a package manager, or any dependency. If a feature seems to require one, raise it instead of implementing it.
