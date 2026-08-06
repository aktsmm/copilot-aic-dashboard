#!/usr/bin/env python3
"""Copilot ローカル AIC ダッシュボード用の集計スクリプト。

処理の流れ:
  1. ~/.copilot/session-store.db（ライブ DB）を読み取り専用で開き、
     追記専用のアーカイブ DB へマージする（aic_archive.py）。
  2. 集計は**アーカイブ側**を参照して行い、data/usage.json / .js を書き出す。

ライブ DB は Copilot 本体が管理していて保持期間やユーザー操作で消えるが、
アーカイブは消えないので、過去の消費履歴が失われない。
標準ライブラリのみを使用する。元 DB には一切書き込まない。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import sys
import tempfile
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import aic_archive

HERE = Path(__file__).resolve().parent
DEFAULT_CONFIG = HERE / "config.json"
DEFAULT_OUT = HERE / "data" / "usage.json"

QUERY = """
SELECT
    u.id, u.session_id, u.turn_index, u.agent_id, u.parent_tool_call_id,
    u.model, u.input_tokens, u.output_tokens, u.cache_read_tokens,
    u.cache_write_tokens, u.reasoning_tokens, u.total_nano_aiu,
    u.request_multiplier, u.duration_ms, u.time_to_first_token_ms,
    u.initiator, u.reasoning_effort, u.created_at,
    s.repository, s.branch, s.summary, s.host_type, s.cwd
FROM usage_events u
LEFT JOIN sessions s ON s.id = u.session_id
"""

WEEKDAY_JA = ["月", "火", "水", "木", "金", "土", "日"]


# --------------------------------------------------------------------------
# 設定 / DB 接続
# --------------------------------------------------------------------------

def load_config(path: Path) -> dict:
    cfg = json.loads(path.read_text(encoding="utf-8"))
    # config.local.json（gitignore 済み）で自分の環境固有の値だけを上書きする
    local = path.with_name(path.stem + ".local" + path.suffix)
    if local.exists():
        cfg.update(json.loads(local.read_text(encoding="utf-8")))
    return {k: v for k, v in cfg.items() if not k.startswith("_")}


def resolve_db_path(cfg: dict) -> Path:
    if cfg.get("db_path"):
        return Path(os.path.expandvars(os.path.expanduser(cfg["db_path"])))
    return Path.home() / ".copilot" / "session-store.db"


# ライブ DB への接続は aic_archive.connect_readonly() に一本化している。
# db / -wal / -shm を個別にコピーする方式は、逐次コピー中にチェックポイントが
# 走ると不整合なスナップショットになるため採用しない。


# --------------------------------------------------------------------------
# 変換ヘルパ
# --------------------------------------------------------------------------

def parse_ts(raw: str | None) -> datetime | None:
    if not raw:
        return None
    text = raw.strip()
    try:
        if text.endswith("Z"):
            return datetime.fromisoformat(text[:-1] + "+00:00")
        dt = datetime.fromisoformat(text)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def num(value, default=0.0) -> float:
    return default if value is None else float(value)


def r(value: float, digits: int = 1) -> float:
    return round(value + 0.0, digits)


def host_label(host_type: str | None) -> str:
    return "App / Scout" if host_type == "github" else "CLI"


# --------------------------------------------------------------------------
# 集計
# --------------------------------------------------------------------------

def build_payload(rows: list[sqlite3.Row], cfg: dict) -> dict:
    tz = timezone(timedelta(hours=cfg["tz_offset_hours"]))
    tz_label = cfg["tz_label"]
    to_aic = cfg["aiu_to_aic"]
    usd = cfg["usd_per_aic"]
    max_chars = cfg["summary_max_chars"]

    events = []
    for row in rows:
        ts = parse_ts(row["created_at"])
        if ts is None:
            continue
        local = ts.astimezone(tz)
        events.append({
            "session_id": row["session_id"],
            "agent_id": row["agent_id"],
            "model": row["model"] or "unknown",
            "initiator": row["initiator"] or "main",
            "effort": row["reasoning_effort"] or "-",
            "aic": num(row["total_nano_aiu"]) / 1e9 * to_aic,
            "mult": num(row["request_multiplier"], 1.0),
            "in_tok": num(row["input_tokens"]),
            "out_tok": num(row["output_tokens"]),
            "cache_r": num(row["cache_read_tokens"]),
            "cache_w": num(row["cache_write_tokens"]),
            "reason_tok": num(row["reasoning_tokens"]),
            "dur_ms": num(row["duration_ms"]),
            "utc": ts,
            "local": local,
            "repository": row["repository"] or "(no repo)",
            "branch": row["branch"] or "",
            "summary": (row["summary"] or "").strip(),
            "host": host_label(row["host_type"]),
        })

    if not events:
        raise SystemExit(
            "利用実績が 1 件もありません。\n"
            "  Copilot CLI / App をまだ使っていないか、ローカル DB が空です。\n"
            "  Copilot を数回使ってから、もう一度実行してください。\n"
            "  データなしで画面を確認したい場合は .\\run-dashboard.ps1 -Demo を実行してください。"
        )

    events.sort(key=lambda e: e["utc"])
    now_local = datetime.now(tz)
    total_aic = sum(e["aic"] for e in events)

    payload = {
        "meta": build_meta(events, cfg, now_local, tz_label, total_aic, usd),
        "kpi": build_kpi(events, cfg, now_local, usd),
        "hourly": build_hourly(events, cfg, now_local),
        "daily": build_daily(events, cfg, now_local),
        "heatmap": build_heatmap(events),
        "models": build_models(events, total_aic),
        "initiators": build_initiators(events, total_aic),
        "efforts": build_efforts(events, total_aic),
        "hosts": build_hosts(events, total_aic),
        "repos": build_repos(events, total_aic),
        "sessions": build_sessions(events, cfg, max_chars),
        "efficiency": build_efficiency(events),
    }
    return payload


def build_meta(events, cfg, now_local, tz_label, total_aic, usd) -> dict:
    return {
        "generated_at": now_local.strftime("%Y-%m-%d %H:%M:%S"),
        "tz_label": tz_label,
        "tz_offset_hours": cfg["tz_offset_hours"],
        "first_event": events[0]["local"].strftime("%Y-%m-%d %H:%M"),
        "last_event": events[-1]["local"].strftime("%Y-%m-%d %H:%M"),
        "event_count": len(events),
        "session_count": len({e["session_id"] for e in events}),
        "total_aic": r(total_aic),
        "total_usd": r(total_aic * usd, 2),
        "aiu_to_aic": cfg["aiu_to_aic"],
        "usd_per_aic": usd,
        "monthly_included_aic": cfg["monthly_included_aic"],
        "daily_budget_aic": cfg["daily_budget_aic"],
        "hourly_alert_aic": cfg["hourly_alert_aic"],
    }


def build_kpi(events, cfg, now_local, usd) -> dict:
    today = now_local.date()
    yesterday = today - timedelta(days=1)
    month_key = today.strftime("%Y-%m")

    def total(pred) -> float:
        return sum(e["aic"] for e in events if pred(e))

    last_1h = total(lambda e: e["local"] >= now_local - timedelta(hours=1))
    last_24h = total(lambda e: e["local"] >= now_local - timedelta(hours=24))
    last_7d = total(lambda e: e["local"] >= now_local - timedelta(days=7))
    today_aic = total(lambda e: e["local"].date() == today)
    yday_aic = total(lambda e: e["local"].date() == yesterday)
    month_aic = total(lambda e: e["local"].strftime("%Y-%m") == month_key)

    # ローリングウィンドウの最大値（レート制限の当たりを見るための指標）
    peak_1h = rolling_peak(events, hours=1)
    peak_24h = rolling_peak(events, hours=24)

    included = cfg["monthly_included_aic"] or 0
    return {
        "today": r(today_aic),
        "today_usd": r(today_aic * usd, 2),
        "yesterday": r(yday_aic),
        "dod_pct": r((today_aic - yday_aic) / yday_aic * 100) if yday_aic else None,
        "last_1h": r(last_1h),
        "last_24h": r(last_24h),
        "last_7d": r(last_7d),
        "avg_daily_7d": r(last_7d / 7),
        "month": r(month_aic),
        "month_usd": r(month_aic * usd, 2),
        "month_pct_of_included": r(month_aic / included * 100) if included else None,
        "peak_1h": r(peak_1h["value"]),
        "peak_1h_at": peak_1h["at"],
        "peak_24h": r(peak_24h["value"]),
        "peak_24h_at": peak_24h["at"],
        "daily_budget_pct": r(today_aic / cfg["daily_budget_aic"] * 100)
        if cfg["daily_budget_aic"] else None,
    }


def rolling_peak(events, hours: int) -> dict:
    """各イベント終端で見た hours 幅のローリング合計の最大値。"""
    window = timedelta(hours=hours)
    best_value, best_at = 0.0, None
    left, running = 0, 0.0
    for right, ev in enumerate(events):
        running += ev["aic"]
        while events[left]["utc"] <= ev["utc"] - window:
            running -= events[left]["aic"]
            left += 1
        if running > best_value:
            best_value = running
            best_at = ev["local"].strftime("%m-%d %H:%M")
    return {"value": best_value, "at": best_at}


def build_hourly(events, cfg, now_local) -> list[dict]:
    span = cfg["hourly_hours"]
    start = (now_local - timedelta(hours=span - 1)).replace(minute=0, second=0, microsecond=0)

    buckets: dict[str, dict] = {}
    for offset in range(span):
        slot = start + timedelta(hours=offset)
        buckets[slot.strftime("%Y-%m-%d %H")] = {
            "label": slot.strftime("%m/%d %H:00"),
            "short": slot.strftime("%H"),
            "date": slot.strftime("%m/%d"),
            "ts": slot.isoformat(),
            "aic": 0.0,
            "calls": 0,
            "models": defaultdict(float),
        }

    for e in events:
        key = e["local"].strftime("%Y-%m-%d %H")
        bucket = buckets.get(key)
        if bucket is None:
            continue
        bucket["aic"] += e["aic"]
        bucket["calls"] += 1
        bucket["models"][e["model"]] += e["aic"]

    out = []
    for bucket in buckets.values():
        top = max(bucket["models"].items(), key=lambda kv: kv[1])[0] if bucket["models"] else None
        out.append({
            "label": bucket["label"],
            "short": bucket["short"],
            "date": bucket["date"],
            "ts": bucket["ts"],
            "aic": r(bucket["aic"]),
            "calls": bucket["calls"],
            "top_model": top,
        })
    return out


def build_daily(events, cfg, now_local) -> list[dict]:
    span = cfg["daily_days"]
    start_day = (now_local - timedelta(days=span - 1)).date()

    buckets: dict[str, dict] = {}
    for offset in range(span):
        day = start_day + timedelta(days=offset)
        buckets[day.isoformat()] = {
            "date": day.isoformat(),
            "label": day.strftime("%m/%d"),
            "weekday": WEEKDAY_JA[day.weekday()],
            "is_weekend": day.weekday() >= 5,
            "aic": 0.0,
            "calls": 0,
            "sessions": set(),
            "peak_hour_aic": defaultdict(float),
        }

    for e in events:
        key = e["local"].date().isoformat()
        bucket = buckets.get(key)
        if bucket is None:
            continue
        bucket["aic"] += e["aic"]
        bucket["calls"] += 1
        bucket["sessions"].add(e["session_id"])
        bucket["peak_hour_aic"][e["local"].hour] += e["aic"]

    series = []
    for bucket in buckets.values():
        peak_hour, peak_val = (None, 0.0)
        if bucket["peak_hour_aic"]:
            peak_hour, peak_val = max(bucket["peak_hour_aic"].items(), key=lambda kv: kv[1])
        series.append({
            "date": bucket["date"],
            "label": bucket["label"],
            "weekday": bucket["weekday"],
            "is_weekend": bucket["is_weekend"],
            "aic": r(bucket["aic"]),
            "calls": bucket["calls"],
            "sessions": len(bucket["sessions"]),
            "peak_hour": peak_hour,
            "peak_hour_aic": r(peak_val),
        })

    # 7 日移動平均
    for i, item in enumerate(series):
        window = [s["aic"] for s in series[max(0, i - 6): i + 1]]
        item["ma7"] = r(sum(window) / len(window))

    # DB にデータが無い先頭の空白期間は切り落とす（グラフの無駄な余白を防ぐ）
    first_used = next((i for i, s in enumerate(series) if s["calls"] > 0), 0)
    return series[first_used:]


def build_heatmap(events) -> list[dict]:
    grid = defaultdict(lambda: {"aic": 0.0, "calls": 0})
    for e in events:
        key = (e["local"].weekday(), e["local"].hour)
        grid[key]["aic"] += e["aic"]
        grid[key]["calls"] += 1
    return [
        {
            "weekday": wd,
            "weekday_label": WEEKDAY_JA[wd],
            "hour": hour,
            "aic": r(cell["aic"]),
            "calls": cell["calls"],
        }
        for (wd, hour), cell in sorted(grid.items())
    ]


def _group(events, key_fn, total_aic, extra=None) -> list[dict]:
    groups = defaultdict(lambda: {"aic": 0.0, "calls": 0, "in_tok": 0.0,
                                  "out_tok": 0.0, "mult": [], "dur": 0.0})
    for e in events:
        g = groups[key_fn(e)]
        g["aic"] += e["aic"]
        g["calls"] += 1
        g["in_tok"] += e["in_tok"]
        g["out_tok"] += e["out_tok"]
        g["mult"].append(e["mult"])
        g["dur"] += e["dur_ms"]

    out = []
    for name, g in groups.items():
        item = {
            "name": name,
            "aic": r(g["aic"]),
            "calls": g["calls"],
            "aic_per_call": r(g["aic"] / g["calls"], 2),
            "share_pct": r(g["aic"] / total_aic * 100) if total_aic else 0,
            "in_tok": int(g["in_tok"]),
            "out_tok": int(g["out_tok"]),
            "avg_mult": r(sum(g["mult"]) / len(g["mult"]), 2),
            "avg_sec": r(g["dur"] / g["calls"] / 1000, 1),
        }
        if extra:
            extra(item, g)
        out.append(item)
    return sorted(out, key=lambda x: x["aic"], reverse=True)


def build_models(events, total_aic) -> list[dict]:
    return _group(events, lambda e: e["model"], total_aic)


def build_initiators(events, total_aic) -> list[dict]:
    return _group(events, lambda e: e["initiator"], total_aic)


def build_efforts(events, total_aic) -> list[dict]:
    return _group(events, lambda e: e["effort"], total_aic)


def build_hosts(events, total_aic) -> list[dict]:
    return _group(events, lambda e: e["host"], total_aic)


def build_repos(events, total_aic) -> list[dict]:
    items = _group(events, lambda e: e["repository"], total_aic)
    return items[:20]


def build_sessions(events, cfg, max_chars) -> list[dict]:
    sessions: dict[str, dict] = {}
    for e in events:
        s = sessions.setdefault(e["session_id"], {
            "id": e["session_id"],
            "repository": e["repository"],
            "branch": e["branch"],
            "host": e["host"],
            "summary": e["summary"][:max_chars],
            "aic": 0.0,
            "calls": 0,
            "in_tok": 0.0,
            "out_tok": 0.0,
            "start": e["local"],
            "end": e["local"],
            "models": defaultdict(float),
            "children": {},
        })
        s["aic"] += e["aic"]
        s["calls"] += 1
        s["in_tok"] += e["in_tok"]
        s["out_tok"] += e["out_tok"]
        s["start"] = min(s["start"], e["local"])
        s["end"] = max(s["end"], e["local"])
        s["models"][e["model"]] += e["aic"]

        if e["agent_id"]:
            child = s["children"].setdefault(e["agent_id"], {
                "id": e["agent_id"][:12],
                "model": e["model"],
                "initiator": e["initiator"],
                "aic": 0.0,
                "calls": 0,
            })
            child["aic"] += e["aic"]
            child["calls"] += 1

    out = []
    for s in sessions.values():
        minutes = max(1, int((s["end"] - s["start"]).total_seconds() // 60))
        children = sorted(s["children"].values(), key=lambda c: c["aic"], reverse=True)
        for c in children:
            c["aic"] = r(c["aic"])
        child_aic = sum(c["aic"] for c in children)
        out.append({
            "id": s["id"][:8],
            "repository": s["repository"],
            "branch": s["branch"],
            "host": s["host"],
            "summary": s["summary"],
            "aic": r(s["aic"]),
            "calls": s["calls"],
            "in_tok": int(s["in_tok"]),
            "out_tok": int(s["out_tok"]),
            "start": s["start"].strftime("%m/%d %H:%M"),
            "end": s["end"].strftime("%m/%d %H:%M"),
            "minutes": minutes,
            "aic_per_hour": r(s["aic"] / (minutes / 60)),
            "top_model": max(s["models"].items(), key=lambda kv: kv[1])[0],
            "child_count": len(children),
            "child_aic": r(child_aic),
            "children": children[:12],
        })
    out.sort(key=lambda x: x["aic"], reverse=True)
    return out[:cfg["top_sessions"]]


def build_efficiency(events) -> dict:
    total_aic = sum(e["aic"] for e in events)
    in_tok = sum(e["in_tok"] for e in events)
    out_tok = sum(e["out_tok"] for e in events)
    cache_r = sum(e["cache_r"] for e in events)
    cache_w = sum(e["cache_w"] for e in events)
    reason = sum(e["reason_tok"] for e in events)
    dur = sum(e["dur_ms"] for e in events)
    cached_share = cache_r / in_tok * 100 if in_tok else 0
    return {
        "in_tok": int(in_tok),
        "out_tok": int(out_tok),
        "cache_read": int(cache_r),
        "cache_write": int(cache_w),
        "reasoning_tok": int(reason),
        "cache_hit_pct": r(cached_share),
        "aic_per_mtok_in": r(total_aic / (in_tok / 1e6)) if in_tok else 0,
        "aic_per_ktok_out": r(total_aic / (out_tok / 1e3), 2) if out_tok else 0,
        "reasoning_share_pct": r(reason / out_tok * 100) if out_tok else 0,
        "total_hours": r(dur / 1000 / 3600),
        "avg_sec_per_call": r(dur / len(events) / 1000, 1),
    }


# --------------------------------------------------------------------------
# 出力とカバレッジ
# --------------------------------------------------------------------------

def write_atomic(path: Path, text: str) -> None:
    """同ディレクトリの一時ファイルへ書いてから差し替える。

    定期実行と手動実行が重なっても、ブラウザが書きかけの usage.js を
    読み込むことがないようにする。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def mark_incomplete(payload: dict, cov: dict, cfg: dict) -> None:
    """記録が無い期間を「0 消費」と誤読させないための印を付ける。

    アーカイブ開始前や、取りこぼしの疑いがある区間に重なるバケットは
    incomplete=True にして、UI 側で網掛け表示する。
    平均系の指標も、対象期間が完全に覆えていなければ partial を立てる。
    """
    tz = timezone(timedelta(hours=cfg["tz_offset_hours"]))

    def to_local(iso):
        dt = parse_ts(iso)
        return dt.astimezone(tz) if dt else None

    since = to_local(cov.get("since"))
    ranges = []
    for g in cov.get("gaps", []):
        lo, hi = to_local(g["from"]), to_local(g["to"])
        if lo and hi:
            ranges.append((lo, hi, g.get("confidence", "low")))

    def status(lo, hi):
        # アーカイブ開始点をまたぐバケットも「一部しか記録が無い」ので印を付ける。
        # hi <= since だけを見ると、最初のイベントを含むバケットが取りこぼされる。
        if since and lo < since:
            return "before_archive"
        for glo, ghi, conf in ranges:
            if lo < ghi and hi > glo:
                return f"gap_{conf}"
        return None

    for b in payload.get("hourly", []):
        lo = datetime.fromisoformat(b["ts"])
        st = status(lo, lo + timedelta(hours=1))
        if st:
            b["incomplete"] = st

    daily = payload.get("daily", [])
    for b in daily:
        lo = datetime.fromisoformat(b["date"]).replace(tzinfo=tz)
        st = status(lo, lo + timedelta(days=1))
        if st:
            b["incomplete"] = st

    # 移動平均を「記録がある日」だけで取り直す。
    # 欠測日を 0 として混ぜると平均が実態より低く出て、
    # 「最近は落ち着いている」という逆の結論を導いてしまう。
    for i, item in enumerate(daily):
        window = [s["aic"] for s in daily[max(0, i - 6): i + 1] if not s.get("incomplete")]
        item["ma7"] = r(sum(window) / len(window)) if window else None
        item["ma7_partial"] = len(window) < min(i + 1, 7)

    now_local = datetime.now(tz)
    kpi = payload.get("kpi", {})
    for key, hours in (("last_24h", 24), ("last_7d", 24 * 7), ("last_30d", 24 * 30)):
        if key not in kpi:
            continue
        lo = now_local - timedelta(hours=hours)
        if status(lo, now_local):
            kpi.setdefault("partial", []).append(key)

    # 7 日平均も、記録のある日だけを分母にする。
    today = now_local.date()
    recent = [
        b for b in daily
        if not b.get("incomplete")
        and 0 <= (today - datetime.fromisoformat(b["date"]).date()).days < 7
    ]
    if recent:
        kpi["avg_daily_7d"] = r(sum(b["aic"] for b in recent) / len(recent))
        kpi["avg_daily_7d_days"] = len(recent)
        if len(recent) < 7:
            kpi.setdefault("partial", []).append("avg_daily_7d")
    else:
        kpi["avg_daily_7d"] = None
        kpi["avg_daily_7d_days"] = 0
        kpi.setdefault("partial", []).append("avg_daily_7d")


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description="Copilot ローカル AIC 使用量を集計する")
    ap.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--source", type=Path, default=None,
                    help="ライブ DB のパス（既定: config.json / ~/.copilot/session-store.db）")
    ap.add_argument("--archive", type=Path, default=None,
                    help="アーカイブ DB のパス（既定: config.json の archive_db）")
    ap.add_argument("--no-archive", action="store_true",
                    help="ライブ DB からアーカイブへの取り込みを行わず、既存アーカイブだけで集計する")
    ap.add_argument("--redact-paths", action="store_true",
                    help="出力に含まれるローカルパスを伏せる（デモや共有用）")
    ap.add_argument("--quiet", action="store_true")
    ap.add_argument("--no-alert", action="store_true",
                    help="閾値を超えていても通知しない")
    args = ap.parse_args()

    cfg = load_config(args.config)
    arc_cfg = aic_archive.load_config(HERE)
    archive_path = Path(args.archive) if args.archive else Path(
        os.path.expandvars(os.path.expanduser(arc_cfg["archive_db"])))
    live_path = Path(args.source) if args.source else resolve_db_path(cfg)

    # 1) ライブ DB → アーカイブへ追記マージ。
    #    ライブ DB が消えている / ロックされていてもアーカイブがあれば集計は続行する。
    ingest = None
    if not args.no_archive:
        arc = aic_archive.open_archive(archive_path)
        try:
            integrity = aic_archive.check_integrity(arc)
            if integrity != "ok":
                print(f"[FATAL] アーカイブ DB が破損しています: {archive_path}\n"
                      f"        integrity_check => {integrity}", file=sys.stderr)
                return 2
            with aic_archive.ProcessLock(archive_path.with_suffix(".lock")):
                ingest = aic_archive.ingest(live_path, arc, args.quiet)
        finally:
            arc.close()

    if not archive_path.exists():
        print(f"[error] アーカイブ DB がありません: {archive_path}\n"
              f"        先に python aic_archive.py を実行してください。", file=sys.stderr)
        return 1

    # 2) アーカイブから集計する（ライブ DB は参照しない）
    arc = sqlite3.connect(f"file:{archive_path.as_posix()}?mode=ro", uri=True, timeout=30)
    try:
        arc.row_factory = sqlite3.Row
        rows = arc.execute(QUERY).fetchall()
        cov = aic_archive.coverage(arc)
        alert_failures = [
            {"kind": k.split(":", 1)[1], "at": v}
            for k, v in arc.execute(
                "SELECT key, value FROM meta "
                "WHERE key LIKE 'alert_delivery_failed:%' AND value <> ''")
        ]
    finally:
        arc.close()

    payload = build_payload(rows, cfg)
    payload["meta"].update({
        "db_path": str(archive_path),
        "live_db_path": str(live_path),
        "live_db_present": live_path.exists(),
        "project_dir": str(HERE),
        "gaps": cov["gaps"],
        "archive_runs": cov["runs"],
        "archive_failed_runs": cov["failed_runs"],
        "archive_quarantined": cov["quarantined"],
        "archive_last_run_at": cov["last_run_at"],
        "archive_last_run_status": cov["last_run_status"],
        "archive_since": cov["since"],
        "archive_local_since": cov.get("local_since"),
        "machines": cov.get("machines", []),
        "alert_failures": alert_failures,
        "ingested": ingest,
    })
    mark_incomplete(payload, cov, cfg)

    if args.redact_paths:
        for key in ("db_path", "live_db_path", "project_dir"):
            payload["meta"][key] = "(redacted)"
        # マシン名はホスト名そのもの。デモ用出力に混ぜてはいけない。
        for i, m in enumerate(payload["meta"].get("machines", []), 1):
            m["origin"] = "this machine" if m.get("is_local") else f"machine-{i}"
        for g in payload["meta"].get("gaps", []):
            if g.get("origin"):
                g["origin"] = "another machine"
        for mm in payload["meta"].get("machines", []):
            mm.pop("merged_at", None)
        payload["meta"]["demo"] = True

    # 同ディレクトリの一時ファイルへ書いてから os.replace で差し替える。
    # 定期実行と手動実行がぶつかっても、ブラウザが半端な JS を読むことがない。
    write_atomic(args.out, json.dumps(payload, ensure_ascii=False, indent=1))
    # ダッシュボードを単体ファイルで開けるよう JS 版も出力する（file:// 対応）
    write_atomic(args.out.with_suffix(".js"),
                 "window.AIC_DATA = " + json.dumps(payload, ensure_ascii=False) + ";")

    if not args.quiet:
        m, k = payload["meta"], payload["kpi"]
        print(f"[ok] {m['event_count']} イベント / {m['session_count']} セッション")
        print(f"     期間: {m['first_event']} 〜 {m['last_event']} ({m['tz_label']})")
        print(f"     合計: {m['total_aic']:,.1f} AIC (${m['total_usd']:,.2f})")
        print(f"     今日: {k['today']:,.1f} AIC / 直近24h: {k['last_24h']:,.1f} AIC")
        print(f"     ピーク 1h: {k['peak_1h']:,.1f} AIC @ {k['peak_1h_at']}")
        print(f"     ピーク 24h: {k['peak_24h']:,.1f} AIC @ {k['peak_24h_at']}")
        print(f"[ok] 出力: {args.out}")

    # 3) 閾値超過の通知。定期実行はこのスクリプトを叩くので、ここに置く。
    #    通知で落ちても集計結果は既に書き終わっているので、失敗しても 0 を返す。
    if not args.no_alert and not args.redact_paths:
        try:
            import aic_alert
            arc = aic_archive.open_archive(archive_path)
            try:
                # 手動実行と定期実行が重なっても二重に鳴らさない。
                with aic_archive.ProcessLock(archive_path.with_suffix(".alert.lock")):
                    aic_alert.fire(arc, {**arc_cfg, **cfg}, quiet=args.quiet)
            finally:
                arc.close()
        except Exception as exc:                       # noqa: BLE001
            print(f"[warn] 通知の処理に失敗しました: {exc}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
