# GitHub Copilot instructions

This repository follows [`AGENTS.md`](../AGENTS.md). Read it before making changes.

Highest-priority rules, repeated here because breaking them causes silent data loss:

- The live Copilot DB (`~/.copilot/session-store.db`) is **read-only**: `file:...?mode=ro` + `PRAGMA query_only=ON`. Never copy its `db`/`-wal`/`-shm` files. All reads go through `aic_archive.connect_readonly()`.
- The archive is **append-only**. No `DELETE`/`DROP` on `usage_events`, `sessions`, `collect_runs`, `quarantine_events`. `--stats`/`--backup-to`/`--export-csv` must fail rather than create an empty archive.
- Do not change the archive key `(session_id, id, created_at)`.
- **Standard library only.** No new dependencies, no bundler, no framework, no network calls.
- Never commit real usage data. `data/`, `export/`, `sample/`, `*.db`, `*.csv`, `docs/*.png`, `config.local.json` are gitignored; demos come from `tools/make_sample_db.py`. Machine-specific config goes in `config.local.json`, never `config.json`.
- Missing data renders as a shaded band, never as a zero bar, and is excluded from moving averages and `avg_daily_7d`.
- Retry only BUSY/LOCKED errors; anything else is `UnsupportedSchema`.
- User-facing changes must update **both** `README.md` and `README.ja.md`.

Verify with: `python aic_archive.py` twice (second run must report 0 new / 0 changed), `python aic_collect.py`, `node --check` on each inline `<script>` in `index.html`, and `.\run-dashboard.ps1 -Demo -NoOpen`.
