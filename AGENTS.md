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
13. **Merging another machine's archive must stay append-only and idempotent.** `merge_archive()` uses `INSERT OR IGNORE` only. The `usage_events` primary key deliberately excludes `origin`, which is what makes double-counting impossible when the same event exists in two archives — do not add `origin` to the key. `collect_runs.run_id` is `AUTOINCREMENT` and must be renumbered on import, deduplicated on `(origin, ran_at, status, source_path)`.
14. **Gap detection must be partitioned by `origin`.** `detect_gaps()` infers a replaced source from `source_ident` changing between consecutive runs. Interleaving two machines' runs in one timeline flips that identity on every row and reports *every* run as a high-confidence gap. Any change to gap detection must keep the per-origin split and be re-tested with two synthetic archives.
15. **Alerts must be deduplicated by a bucket, and must never be silent about failing.** Re-notify at most once per bucket; escalate only when the *measured amount* doubles, never on a fixed timer that trains the user to ignore it. Escalation must not be expressed as a ratio to the threshold: the threshold is recomputed from the baseline on every run, so a stored ratio is not comparable to a later one. If the toast cannot be shown, print to stdout — never swallow the alert. Alert state lives in `meta`, which is the only mutable table.
16. **Machine labels are hostnames — treat them as personal data.** `--redact-paths` must replace every `origin` in both `machines` and `gaps` before anything is written to `docs/demo/`.

| Path | Role |
| --- | --- |
| `aic_archive.py` | Archive: schema, migration, merge, gap detection, backup, CSV, reconciliation, multi-machine merge, process lock |
| `aic_collect.py` | Aggregation into `data/usage.json` + `data/usage.js` |
| `aic_alert.py` | Budget thresholds → desktop notification, with dedup state in `meta` |
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
- Merging is transitive. When A merges B and B already contains C's rows, the incoming `origin` must be preserved and only `NULL` rows get the new label. Overwriting turns C's spending into B's, which corrupts both the per-machine breakdown and gap detection.
- Anything that means "how complete is *my* record" must be scoped to `origin IS NULL`: `archive_max_created_before`, every archive-side query in `reconcile()` (it compares against the local live DB), and `coverage()`'s run counters. A merged remote archive otherwise hides local gaps.
- `detect_gaps()` must run per origin. `source_ident` changing between consecutive runs means "the source DB was recreated", so interleaving two machines' runs makes every row look like a new identity and flags the entire history as high-confidence loss.
- Never advance alert dedup state on a failed notification. The scheduled task runs with `--quiet`, so a swallowed failure means the alert is gone forever. Record the failure, retry next run, and surface it in the dashboard.
- `powershell -Command "<script>" -Title x` does not bind `-Title` to the script's `param()`. Pass values through environment variables.
- The machine label is the hostname, i.e. personal data. `--redact-paths` must scrub it from `machines[].origin` *and* `gaps[].origin`.
- `math.log2` on a threshold near zero overflows. Compute tiers as a difference of logs, check `isfinite`, and clamp.
- The alert baseline must be built from the *same* measure the alert uses — a rolling window, not calendar buckets. Rolling sums run hotter, so a bucket-derived baseline silently over-fires.
- Never let the in-progress window into its own baseline, and never use a mean. Both let a spike raise the bar it has to clear, so the tool goes quiet exactly when usage is worst.
- ~80% of hours have zero spend. Any baseline that includes idle windows collapses to near zero and fires on every working hour.
- Do not compare `created_at` against `datetime('now', '-30 days')`. `created_at` is `2026-07-07T05:53:00.000Z` (T-separated, `Z`) while SQLite returns `2026-07-07 05:53:00`, so the string comparison is off by the separator. Use `_iso(now - timedelta(...))`.
- Percentile-to-firing-rate cannot be reasoned about on paper: exceedances persist and cluster, and a trailing baseline lags rising usage. On real data p95 predicted ~7 notifications/month and produced 49. `--tune` exists because of this; keep it honest by recomputing the baseline as of each simulated point rather than once at the end.
- Caching the baseline "once per calendar day" during a backtest is not per-point. It leaks hindsight into that day's earlier samples and makes the result depend on the wall-clock time you ran the command. `tune()` now precomputes one sample grid anchored to `now` and slices `[t - baseline_days, t - window]` out of it per simulated point; that reproduces `_rolling_values(arc, t, ...)` exactly (asserted against it) and is faster than the cache it replaced.
- A backtest that falls back to a default on a cache miss hides the bug instead of failing. The old `dist.get(key, ([], 0.0))` silently scored the whole trailing partial day against the fixed floor, and only for archives roughly 7–21 days long — precisely the new users who run `--tune`.
- Escalation state stores the amount, not the tier. While usage is heavy, that day's own closed windows enter the trailing distribution and raise the threshold, so a tier would stay flat or fall as usage climbs and the escalation would be swallowed. The reverse drift (baseline dips, or `enough` flips) would mint a spurious "it got worse". `_tier()` is display-only now; do not reintroduce it into dedup. Legacy `"{period}|{tier}"` state is treated as "already notified this period" so upgrading cannot fire a false alarm.
- Known, accepted asymmetry: rows whose `created_at` `_parse_ts` cannot read are excluded from the baseline but still counted by `_sum_since` (SQL string compare, no parse). Aligning them by filtering the numerator would drop real spend from the alert, which is the worse failure. Fix it at ingest if it ever matters.
- The scheduled task must run `pythonw.exe`, and every subprocess it spawns needs `CREATE_NO_WINDOW`. `python.exe` is a console app, so each run pops a window and takes focus. It looks trivial in a review and is the single most likely reason a user uninstalls a background collector.
- `schtasks /SC HOURLY /MO <n>` cannot express sub-hour intervals. Rounding `IntervalMinutes / 60` silently registers a coarser schedule than the user asked for; switch to `/SC MINUTE` below 60.
- Staleness is measured against the observed collection cadence (`collect_cadence_min`, the median gap between recent `collect_runs`), not a constant. The old fixed 30-minute cut-off marked every dashboard "stale" the moment the user moved the task to 3 hours. Use the median, not the mean: manual back-to-back runs and multi-day laptop shutdowns both appear in that table.
- Time-relative KPIs must carry the collection time once the data is not fresh. With collection every few hours, an un-annotated "last 1 hour: 0" reads as "I used nothing", which is the exact misreading this dashboard exists to prevent.
- The 1-hour KPI shows the *effective* alert line (baseline when `enough`, otherwise the fixed floor). Printing `hourly_alert_aic` there contradicted the baseline banner further down the page whenever the baseline was higher, which is the normal case.
- Cadence is measured from scheduled runs only (`run_trigger = 'scheduled'`, set by `--scheduled`, which `-InstallTask` now passes). Ad-hoc runs cluster while you are debugging — the real log had 0.2 / 0.5 / 8-minute gaps from one afternoon of testing — so mixing them in measures how many times you just ran it, not how often it normally runs. When there are too few scheduled samples `collect_cadence()` falls back to all runs and reports `source` as `"measuring"` (a scheduled run in the last 24h, so the task is alive and samples are accumulating) or `"unknown"` (no recent scheduled run: never installed, installed before `--scheduled`, or stopped). The two need different next actions from the user, so do not collapse them.
- A `source` other than `"scheduled"` must never *tighten* the stale cut-off. Ad-hoc-polluted measurements read short, so using one would call a healthy 3-hourly setup stale within 45 minutes — the exact bug the cadence measurement was added to fix. `freshness()` floors the limit at the 90-minute default unless the measurement came from scheduled runs alone.
- Cadence counts failed runs. The question is how often the task *fires*, not how often it succeeds; filtering on `status='ok'` makes an hourly task that fails every other run look like a two-hourly one, which loosens the stale threshold in proportion to how broken collection is. Whether ingest is actually working is tracked separately by `last_ok_at`.
- `--scheduled` is self-reported, so it must be corroborated. Passing it by hand records ad-hoc runs as automatic ones and destroys the very measurement it feeds — during development this pulled the measured interval down to 5 minutes on a 3-hourly task, and the polluted rows had to be reset. `_looks_scheduled()` requires `pythonw.exe` on Windows (what `-InstallTask` registers, and nobody invokes by hand because it prints nothing) and no TTY elsewhere (cron, systemd). `sys.stdout.isatty()` alone is not enough: any pipe or redirect defeats it, and every run launched from a CLI agent is piped.
- `trigger` is a reserved word in SQLite. `_ensure_columns` and `_log_run` both emit unquoted identifiers, so a column named `trigger` fails at `ALTER TABLE` and again at `INSERT`. The column is `run_trigger`.
- `--no-archive` opens the archive read-only and therefore never runs `_ensure_columns()`. Any new column must be probed with `PRAGMA table_info` before it is referenced, or that path raises `no such column` on every pre-existing archive — and the exception escapes the `try/finally`.
- Freshness is judged by the last *successful* ingest (`coverage()["last_ok_at"]` → `meta.archive_last_ok_local`), not by `generated_at`. Aggregation still completes when the live DB is locked or gone, so keying off generation time labels hours-old data "just now" while the failure banner right above it says collection is broken.
- Timestamps handed to the UI must be local `"%Y-%m-%d %H:%M:%S"` like `generated_at`. Archive columns are UTC `"...Z"`; passing one through unconverted shifts the display by the timezone offset. `_local_stamp()` in `aic_collect.py` does the conversion.
- Collection interval and chart granularity are independent, and users assume they are not. Buckets are always hourly because events are ingested with their own timestamps; a longer interval only delays how soon recent hours appear. Say so in the README rather than letting someone pick a 1-hour schedule for a resolution they already have.

## Out of scope

Do not add: telemetry, cloud sync, a web server, auth, a package manager, or any dependency. If a feature seems to require one, raise it instead of implementing it.
