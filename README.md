# Copilot Local AIC Dashboard

[日本語 README](README.ja.md) · [Live demo (synthetic data)](https://aktsmm.github.io/copilot-aic-dashboard/demo/)

A local, read-only dashboard for your own **GitHub Copilot AI Credits (AIC)** consumption — with **hourly granularity**, which the official reports do not provide.

> **Why hourly?** GitHub's official usage reports are daily. If you are hitting rate limits, the useful question is *"how much did I burn in a single hour?"* — this tool answers that.

![Dashboard overview](docs/demo/screenshot-light.png)

---

## The problem this solves

`~/.copilot/session-store.db` holds a rich per-request usage log written by Copilot CLI / the Copilot app. But:

1. **It is ephemeral.** Copilot prunes it, and many people clear it manually. When it goes, your history goes.
2. **It is not aggregated.** Raw rows, no time bucketing, no cost view.
3. **Official reports are daily only.** You cannot see an hourly burst.

This project:

- **Archives** every usage row into an **append-only database outside `~/.copilot`**, so clearing the local DB never loses history.
- **Aggregates** from the archive into a single self-contained HTML dashboard.
- **Flags gaps** where history may have been lost, instead of silently rendering them as zero usage.

```
~/.copilot/session-store.db  ──(read-only)──▶  archive.db  ──▶  data/usage.js  ──▶  index.html
       may be deleted anytime                 never deleted        regenerated        static file
```

---

## Quick start

**Requirements:** Windows + PowerShell 5.1/7+, Python 3.9+. No pip packages needed.

```powershell
git clone https://github.com/aktsmm/copilot-aic-dashboard.git
cd copilot-aic-dashboard
.\setup.ps1
```

`setup.ps1` checks prerequisites, picks an archive location (default `~/.copilot-aic/archive.db`), runs the first collection, and registers an hourly scheduled task so nothing is lost.

Just want to see what it looks like, with no data of your own?

```powershell
.\run-dashboard.ps1 -Demo
```

### Day-to-day

```powershell
.\run-dashboard.ps1                      # collect + open the dashboard
.\run-dashboard.ps1 -Stats               # archive statistics only
.\run-dashboard.ps1 -Verify              # also re-verify the AIU→AIC conversion
.\run-dashboard.ps1 -BackupTo D:\backup  # safe online backup of the archive
.\run-dashboard.ps1 -ExportCsv .\out.csv # export every archived event
.\run-dashboard.ps1 -UninstallTask       # stop automatic collection
```

Cross-platform / no PowerShell:

```bash
python aic_collect.py          # collect and regenerate data/usage.js
python aic_archive.py --stats  # archive statistics
open index.html                # any browser, file:// works
```

---

## What you get

| Panel | Question it answers |
| --- | --- |
| **KPIs** | Today, last 1h, last 24h, month-to-date, all-time peak 1h / 24h (rolling) |
| **Burn rate (hourly)** | How hard did I hit it in a single hour? Where is the spike? |
| **Daily trend** | 7-day moving average vs. your daily budget line |
| **Hour × weekday heatmap** | When am I actually consuming credits? |
| **By model / initiator / effort / host** | Where is the money going? |
| **By repository** | Which project is expensive? |
| **Session table** | Top sessions with summary, model mix, child-agent share |
| **Efficiency** | Cache hit rate, AIC per 1M input tokens, reasoning token share |

Everything is one static HTML file plus a generated `data/usage.js`. No server, no build step, no telemetry.

---

## Safety guarantees

- **The live Copilot DB is opened read-only** (`file:...?mode=ro` + `PRAGMA query_only=ON`). It is never modified, and the WAL trio is never copied (which could produce an inconsistent snapshot). If it is locked, the run retries with backoff and then skips ingestion rather than writing garbage.
- **The archive is append-only.** No `DELETE`, no `DROP` on usage rows. Schema migrations rename the old table aside instead of dropping it.
- **Nothing leaves your machine.** No network calls anywhere in the codebase.
- **`data/`, `export/` and `sample/` are gitignored** so your own usage never gets committed.

### Key design: why `(session_id, id, created_at)`

`assistant_usage_events.id` is `INTEGER PRIMARY KEY AUTOINCREMENT`, so it **restarts at 1 when the DB is recreated**.

| Candidate key | Verdict |
| --- | --- |
| `id` alone | ✗ After recreation, a different event overwrites an existing archived row — silent, unrecoverable data loss. |
| `(session_id, id)` | ✗ A restored/resumed session can reappear with the same UUID in a new DB generation, and then collide the same way. |
| Content hash | ✗ Any minor column update produces a new key → double counting. |
| **`(session_id, id, created_at)`** | ✓ All three are immutable identity fields. Re-scanning the same DB is idempotent; a reused `id` after a reset lands as a separate row. |

Rows without a usable `created_at` are moved to a `quarantine_events` table rather than being dropped or guessed.

### Honest gap reporting

Event timestamps alone cannot prove that a DB was deleted — a quiet stretch looks identical to a lost one. So each collection run records the **source file generation** (`st_ino` / ctime / size) and every run is logged, including failures. Gaps are then reported with a confidence level:

- **high** — the source file was replaced *and* the timeline is discontinuous.
- **low** — only the timeline is discontinuous; you may simply not have used Copilot.

Affected buckets are drawn as **shaded bands, not zero-height bars**, and averages that span a gap are marked with ⚠ so you never mistake "no record" for "no usage".

---

## How AIC is calculated

The DB stores `total_nano_aiu`. Empirically (verified against official per-model token prices):

```
AI Credits = total_nano_aiu / 1e9
1 AIC      = 1 AIU = USD 0.01
```

And the underlying charge reconstructs as:

```
AIC = ( (input_tokens − cache_read − cache_write) × input_price
      + cache_read  × cached_input_price
      + cache_write × cache_write_price
      + output_tokens × output_price ) / 1e6 × 100
```

Note that `input_tokens` **includes** the cached portions. `verify_pricing.py` re-derives this from your own data and prints the per-model deviation; a persistent deviation usually means the model's price changed during the period, not that the formula is wrong.

> These figures are observations, not official guarantees. Your invoice is the source of truth.

---

## Where are the rate limits?

Short answer: **GitHub does not publish the numbers.** The documentation states only that rate limits exist and are temporary. What the docs *do* say is worth knowing:

- Limits are applied to protect the service, and "if you're making frequent or automated requests, consider adjusting your usage pattern."
- Running out of **credits** (budget/quota exhaustion) is a different thing from being **rate limited**.
- For Business/Enterprise, credits are pooled across all licensed seats.
- Official visibility stops at **daily** granularity, and there is no built-in threshold alerting.

That is exactly the gap this dashboard fills: it lets you see your own hourly peaks, so you can correlate them with the throttling you actually experience.

See [README.ja.md](README.ja.md#7-どのくらいで制限か--公式ドキュメントの回答) for the detailed documentation walkthrough, and the [official references](#official-references) below.

---

## Configuration (`config.json`)

| Key | Default | Meaning |
| --- | --- | --- |
| `db_path` | `null` | `null` auto-detects `%USERPROFILE%\.copilot\session-store.db` |
| `archive_db` | `~/.copilot-aic/archive.db` | Append-only archive. Override with the `AIC_ARCHIVE_DB` env var. |
| `tz_offset_hours` / `tz_label` | `9` / `"JST"` | Timezone used for bucketing |
| `usd_per_aic` | `0.01` | USD per credit |
| `monthly_included_aic` | `3000` | Credits included in your plan |
| `daily_budget_aic` | `5000` | Dashed guideline on the daily chart |
| `hourly_alert_aic` | `1000` | Warning line on the hourly chart |
| `daily_days` / `hourly_hours` | `45` / `96` | Display window |
| `top_sessions` | `40` | Rows in the session table |
| `summary_max_chars` | `60` | Session summary truncation in the generated JSON |

> Full session summaries stay in the local archive; only truncated text is written to `data/`.

`config.json` is shared. To override machine-specific paths without touching it, create **`config.local.json`** (gitignored) with the same shape:

```json
{ "archive_db": "D:\\backup\\copilot\\archive.db" }
```

Precedence: `AIC_ARCHIVE_DB` env var > `config.local.json` > `config.json` > defaults.

Keep the archive **outside** `~/.copilot`. Anything inside that folder can be swept away by Copilot's own cleanup, taking your archive with it.

---

## Repository layout

```
setup.ps1              first-time setup (prereqs → config → first run → scheduled task)
run-dashboard.ps1      day-to-day driver (-Stats -Verify -Demo -InstallTask -BackupTo -ExportCsv)
aic_archive.py         append-only archive: merge, migrate, gap detection, backup, CSV
aic_collect.py         aggregation → data/usage.json + data/usage.js
verify_pricing.py      re-derives AIC from token counts and official prices
index.html             the dashboard (single file, no dependencies)
config.json            settings
tools/make_sample_db.py  synthetic session-store.db generator for demos/tests
docs/demo/             published demo built from synthetic data
AGENTS.md              instructions for AI coding agents working on this repo
```

---

## Limitations

- **This machine only.** Other devices, and cloud agent work that never touched this machine, are not included.
- **History before your first collection cannot be recovered.** Whatever the local DB had already pruned is gone.
- **If you delete the local DB while collection is not running**, that window is lost — the dashboard will flag it rather than hide it. Keep the scheduled task enabled.
- Numbers will not match an organization invoice exactly. Use GitHub's billing pages for anything financial.
- Model prices change. `verify_pricing.py`'s table is a point-in-time snapshot.
- Back the archive up (`-BackupTo`); it is a local SQLite file like any other.

---

## Official references

- [Usage-based billing for organizations and enterprises](https://docs.github.com/en/copilot/concepts/billing/usage-based-billing-for-organizations-and-enterprises)
- [Usage-based billing for individuals](https://docs.github.com/en/copilot/concepts/billing/usage-based-billing-for-individuals)
- [Models and pricing for GitHub Copilot](https://docs.github.com/en/copilot/reference/copilot-billing/models-and-pricing)
- [Usage limits for GitHub Copilot](https://docs.github.com/en/copilot/concepts/usage-limits)
- [Budgets for usage-based billing](https://docs.github.com/en/copilot/concepts/billing/budgets-for-usage-based-billing)
- [Billing reports reference](https://docs.github.com/en/billing/reference/billing-reports)
- [REST API endpoints for Copilot usage metrics](https://docs.github.com/en/rest/copilot/copilot-usage-metrics)
- [GitHub Acceptable Use Policies](https://docs.github.com/en/site-policy/acceptable-use-policies/github-acceptable-use-policies)

---

## License

[CC BY-NC-SA 4.0](LICENSE), with an additional permission for Microsoft Corporation and its affiliates.

This is a personal, unofficial tool. It is not affiliated with, endorsed by, or supported by GitHub or Microsoft.
