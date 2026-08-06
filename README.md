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

`setup.ps1` checks prerequisites, picks an archive location (default `~/.copilot-aic/archive.db`), runs the first collection, and registers an hourly scheduled task so nothing is lost. It writes your machine-specific path to `config.local.json` and leaves the tracked `config.json` untouched.

If PowerShell refuses to run the script (`... is not digitally signed`), allow it for this session only:

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
```

Scheduled-task registration can fail on locked-down machines. That is not fatal — the archive still works. `-InstallTask` first tries `Register-ScheduledTask` and, if the CIM provider is blocked by policy (a common `アクセスが拒否されました` / `Access is denied` on managed corporate devices), automatically falls back to `schtasks.exe`, which usually succeeds without elevation. If both fail it prints the exact program/arguments so you can register the task by hand, and you can always skip it with `.\setup.ps1 -SkipTask` and just run `.\run-dashboard.ps1` when you want fresh numbers.

Just want to see what it looks like, with no data of your own?

```powershell
.\run-dashboard.ps1 -Demo
```

### Day-to-day

```powershell
.\run-dashboard.ps1                      # collect + open the dashboard
.\run-dashboard.ps1 -Stats               # archive statistics only
.\run-dashboard.ps1 -Verify              # also re-verify the AIU→AIC conversion
.\run-dashboard.ps1 -Reconcile           # cross-check the archive against the Copilot app's own totals
.\run-dashboard.ps1 -CheckAlert          # print which budget thresholds are currently exceeded
.\run-dashboard.ps1 -TestAlert           # send a test desktop notification
.\run-dashboard.ps1 -BackupTo D:\backup  # safe online backup of the archive
.\run-dashboard.ps1 -ExportCsv .\export\usage.csv # export every archived event
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

Note that `input_tokens` **includes** the cached portions. `verify_pricing.py` re-derives this from your own archive and prints the per-model deviation; a persistent deviation usually means the model's price changed during the period, not that the formula is wrong.

<details>
<summary>Worked example: a deviation that turned out to be a price cut</summary>

Running the verifier over the author's own archive (~11k events; aggregate counts only, no identifiers) matched official prices within 2% for nearly every model — except one, which sat around 20% off. Grouping that model's ratio by day showed a **staircase, not noise**:

| Period | observed / current official price |
| --- | ---: |
| through 2026-07-14 | ×1.36 – 1.46 |
| 2026-07-15 – 07-29 | ×1.250 (flat) |
| 2026-07-30 | ×1.016 (transition day) |
| from 2026-08-03 | ×1.000 (exact match) |

So the formula was right; the historical rows were simply billed at the older, higher rate. The dashboard uses the stored `total_nano_aiu` directly, so it stays accurate across price changes. Residual 1–2% deviations on other models trace to the long-context tier boundary (prices double above the threshold).

</details>

> These figures are observations, not official guarantees. Your invoice is the source of truth.

---

## Where are the rate limits?

Short answer: **GitHub does not publish the numbers.** As of 2026-08-05, the documentation states only that rate limits exist and are temporary — neither the thresholds nor the window (hourly? daily? rolling?) are public.

What the docs *do* say, and why it matters here:

- The stated reasons for rate limiting are **capacity, high usage, fairness, and abuse mitigation**. The recommended response is to wait, upgrade, or *"if you're making frequent or automated requests (for example, rapid-fire completions or large-scale usage), consider adjusting your usage pattern."* Heavy parallel-agent workflows are explicitly in scope.
- Running out of **credits** is a different state (`blocked`) from being **rate limited**. Overage is billed at $0.01/AIC and is **on by default**; the budget hard-stop is **off by default** except for user-level budgets.
- **There is no threshold alerting.** You find out by being blocked. That alone justifies watching it locally.
- For Business/Enterprise, included credits are **pooled across all licensed seats**, not per person.

| Plan | Included per user/month | Promotional (2026-06-01 – 09-01) |
| --- | ---: | ---: |
| Copilot Business | 1,900 AIC | **3,000 AIC** |
| Copilot Enterprise | 3,900 AIC | **7,000 AIC** |
| Copilot Pro | 1,500 AIC | — |
| Copilot Pro+ | 7,000 AIC | — |
| Copilot Max | 20,000 AIC | — |

Official reporting stops at **daily** granularity in every channel — the summarized report, the detailed report, the AI usage report CSV (`date` / `model` / `username` / `quantity` / …), and the REST metrics API. **No hourly view exists officially.** That is exactly the gap this dashboard fills: you can see your own hourly peaks and correlate them with the throttling you actually experience.

[README.ja.md](README.ja.md#7-どのくらいで制限か--公式ドキュメントの回答) has the full documentation walkthrough in Japanese, including what could *not* be confirmed. See also the [official references](#official-references) below.

---

## Getting warned before you hit the wall

There is no way to detect throttling after the fact — the local store records no 429s, no quota errors, nothing. So the only useful move is a warning *before* you get there.

Every collection run evaluates three thresholds and raises a desktop notification when one is crossed:

| Alert | Threshold | When it fires again |
| --- | --- | --- |
| Last 1 hour | `hourly_alert_aic` | New clock hour, or the overage **doubles** |
| Last 24 hours | `daily_budget_aic` | New local day, or the overage **doubles** |
| Month to date | `monthly_included_aic × monthly_alert_ratio` | New month, or the overage **doubles** |

The "doubles" rule matters: a fixed once-a-day notification is easy to ignore, and a per-run notification is noise. Re-notifying only when the overage doubles means the alert stays quiet while things are merely bad, and speaks up again when they get materially worse.

Within a period the alert remembers the **highest** level it has already reported. If your rolling-24h number doubles, drops back, and climbs again, you get one notification, not three.

```powershell
.\run-dashboard.ps1 -TestAlert    # verify notifications work at all
.\run-dashboard.ps1 -CheckAlert   # print current values vs thresholds, no notification, no state change
```

Notifications use the Windows toast API through PowerShell — no extra packages. If a toast cannot be shown, the message is printed to stdout instead; it is never silently dropped.

**A notification that fails to display is not counted as delivered.** The alert state is only advanced after a successful notification, so a transient failure is retried on the next run rather than swallowed — and the dashboard shows a banner listing anything that could not be delivered. Set `"alerts_enabled": false` to turn the whole thing off.

Dedup state lives in the archive's `meta` table (`alert_state:*`), so restarting or re-running does not re-trigger.

---

## Multiple machines

The archive is per-machine by design: each machine collects from its own local store. To see the combined picture, copy one archive to the other machine and merge it in.

```powershell
python aic_archive.py --merge-archive D:\from-laptop\archive.db
python aic_archive.py --merge-archive D:\from-laptop\archive.db --origin laptop   # explicit label
```

Merging is **append-only and idempotent**:

- `usage_events` is keyed on `(session_id, id, created_at)`, which does **not** include the machine label. If both archives contain the same event, it is stored once. Merging the same file twice adds nothing.
- Existing rows are never updated or deleted. Only newly inserted rows get tagged with an origin.
- `collect_runs.run_id` is `AUTOINCREMENT`, so runs are renumbered on the way in and deduplicated on `(origin, ran_at, status, source_path)`.

Gap detection runs **per machine**. This is not cosmetic: gaps are inferred partly from the source database's file identity changing between runs, and if two machines' runs were interleaved in one timeline, that identity would flip on every row and every single run would be reported as a high-confidence gap. Splitting by origin is what keeps the report meaningful.

Once more than one machine is present, the dashboard shows a per-machine breakdown and labels each gap with the machine it belongs to.

**What you are looking at is a snapshot, not a live view.** A merged archive is frozen at the moment you exported it; the other machine keeps spending afterwards. So for any period after that machine's last collection, the numbers contain only *this* machine — totals, alerts and charts all read low. The dashboard shows each machine's last collection time next to its subtotal so you can see how stale the picture is. To catch up, collect on the other machine and merge again.

For the same reason, alerts are evaluated against whatever is currently in the archive. If you rely on alerts and use several machines heavily, run the alert check on each machine rather than trusting one merged view.

> The label defaults to the source archive's hostname. It is never written to `docs/demo/` — the demo generator replaces it, along with every path.

---


| Key | Default | Meaning |
| --- | --- | --- |
| `db_path` | `null` | `null` auto-detects `%USERPROFILE%\.copilot\session-store.db` |
| `archive_db` | `~/.copilot-aic/archive.db` | Append-only archive. Override with the `AIC_ARCHIVE_DB` env var. |
| `app_db` | `~/.copilot/data.db` | Read-only cross-check source for `-Reconcile`. Optional; skipped if absent. |
| `tz_offset_hours` / `tz_label` | `9` / `"JST"` | Timezone used for bucketing |
| `aiu_to_aic` | `1.0` | AIU→AIC conversion factor. Verified empirically; you should not need to change it. |
| `usd_per_aic` | `0.01` | USD per credit |
| `monthly_included_aic` | `3000` | Credits included in your plan |
| `daily_budget_aic` | `5000` | Dashed guideline on the daily chart |
| `hourly_alert_aic` | `1000` | Warning line on the hourly chart, and the 1-hour alert threshold |
| `alerts_enabled` | `true` | Desktop notification when a threshold is crossed. Set `false` to disable. |
| `monthly_alert_ratio` | `0.8` | Notify once month-to-date exceeds this fraction of `monthly_included_aic` |
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
run-dashboard.ps1      day-to-day driver (-Stats -Verify -Reconcile -CheckAlert -TestAlert -Demo -InstallTask -BackupTo -ExportCsv)
aic_archive.py         append-only archive: merge, migrate, gap detection, backup, CSV, multi-machine merge
aic_collect.py         aggregation → data/usage.json + data/usage.js
aic_alert.py           budget thresholds → desktop notification (deduplicated)
verify_pricing.py      re-derives AIC from token counts and official prices
index.html             the dashboard (single file, no dependencies)
config.json            settings
tools/make_sample_db.py  synthetic session-store.db generator for demos/tests
docs/demo/             published demo built from synthetic data
AGENTS.md              instructions for AI coding agents working on this repo
```

---

## What this can and cannot see

The dashboard reads the local Copilot CLI store. That covers Copilot CLI runs on this machine — including the ones the Copilot desktop app spawns in worktrees — but not everything you are billed for.

| Source of usage | Covered | Why |
| --- | --- | --- |
| Copilot CLI on this machine | **Yes** | Written to `~/.copilot/session-store.db` with per-event `total_nano_aiu` |
| Copilot app project sessions (worktrees) | **Yes** | These run the CLI locally, so they land in the same store |
| Sub-agents and compaction | **Yes** | Recorded as separate events, attributed via `initiator` |
| **Copilot Coding Agent** | **No** | Runs on GitHub's servers. No usage rows exist locally |
| **Copilot Code Review** | **No** | Same — server-side |
| **VS Code Copilot Chat** | **No** | Separate store with no usage table at all |
| **Other machines** | Only if merged | Each machine keeps its own store. Bring the archive over and see [Multiple machines](#multiple-machines) |

This is not a bug that can be fixed locally: for server-side agents there is simply no local record of consumption to read. If you lean on Coding Agent, your real spend is higher than what this dashboard shows, and only GitHub's billing pages will reflect it.

<details>
<summary>How this was verified</summary>

Copilot also keeps a cloud-side session store, which is why you might expect cross-device data. It does not help here:

- It has no `total_nano_aiu`. It *does* carry input/output/cache-read/cache-write token counts, so AIC can be **re-derived** from it — and on a day where both stores held the same events, the derived total matched the local ground truth exactly (206 vs 206 AIC).
- But its coverage is **incomplete and unpredictable**. Deriving AIC across a 10-day window recovered only **55%** of actual, ranging from 41% to 100% per day. It is a lagging partial mirror, so a cloud-derived number is strictly worse than the local one.
- For Coding Agent and Code Review it carries **zero** usage rows at all — 6,697 and 122 events over 30 days, none with tokens. The blind spots above are therefore not fixable from the cloud either.
- It uses a **disjoint session ID space** from the local store — no ID matches in either direction — so the two cannot be merged per session.

The one thing it could add is usage from *other machines*, since those sessions do appear there. Even then you would be adding an undercount of unknown size to an exact local figure, so this tool does not do it.

</details>

### Cross-checking your archive

If you use the Copilot desktop app, it keeps its own per-session total in `~/.copilot/data.db` (`sessions.total_nano_aiu`). That is written independently of the CLI store, which makes it a useful second opinion:

```powershell
.\run-dashboard.ps1 -Reconcile
```

It reports sessions the archive is missing entirely and sessions where the archive holds *less* than the app does — the two signals that actually indicate lost data. The archive normally totals **more** than the app, because it counts sub-agent and compaction events individually; that direction is expected and is called out as such. If `data.db` does not exist, or its schema differs, the command says so and exits 0 rather than pretending to have verified anything.

---

## Limitations

- **It reads an undocumented internal schema.** `~/.copilot/session-store.db` is a private implementation detail of GitHub Copilot. Its tables, columns, and the meaning of `total_nano_aiu` are not documented, not guaranteed, and can change or disappear in any Copilot update without notice. When that happens the tool reports `unsupported_schema` and stops ingesting — your existing archive is untouched, but new data will not be collected until the tool is updated.
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

This is **source-available, not OSI open source** — non-commercial use only, and derivatives must be shared alike. Creative Commons does not recommend its licenses for software, so if you need a conventional open-source license for reuse, open an issue rather than assuming one.

This is a personal, unofficial tool. It is not affiliated with, endorsed by, or supported by GitHub or Microsoft.
