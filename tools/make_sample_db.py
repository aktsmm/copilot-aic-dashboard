"""
make_sample_db.py — デモ用のダミー session-store.db を生成する。

自分の実データを使わずにダッシュボードの見た目を確認したいとき、
または動作確認・スクリーンショット撮影に使う。

    python tools/make_sample_db.py --out sample/session-store.db

生成される DB は Copilot 本体のスキーマ（assistant_usage_events / sessions）の
うち、このダッシュボードが読む列だけを持つ。実在の個人情報は一切含まない。
"""

from __future__ import annotations

import argparse
import random
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id          TEXT PRIMARY KEY,
    cwd         TEXT,
    repository  TEXT,
    branch      TEXT,
    summary     TEXT,
    created_at  TEXT,
    updated_at  TEXT,
    host_type   TEXT
);
CREATE TABLE IF NOT EXISTS assistant_usage_events (
    id                       INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id               TEXT NOT NULL,
    turn_index               INTEGER,
    agent_id                 TEXT,
    parent_tool_call_id      TEXT,
    model                    TEXT,
    input_tokens             INTEGER,
    output_tokens            INTEGER,
    cache_read_tokens        INTEGER,
    cache_write_tokens       INTEGER,
    reasoning_tokens         INTEGER,
    total_nano_aiu           INTEGER,
    request_multiplier       REAL,
    duration_ms              INTEGER,
    time_to_first_token_ms   INTEGER,
    inter_token_latency_ms   INTEGER,
    initiator                TEXT,
    api_endpoint             TEXT,
    reasoning_effort         TEXT,
    finish_reason            TEXT,
    content_filter_triggered INTEGER,
    token_details_json       TEXT,
    created_at               TEXT
);
"""

# 1M トークンあたりの USD。AIC 換算は 1 AIC = $0.01。
PRICING = {
    "demo-large":  {"in": 1.25, "cached": 0.125, "cwrite": 1.25, "out": 10.00},
    "demo-medium": {"in": 0.60, "cached": 0.060, "cwrite": 0.60, "out": 4.00},
    "demo-small":  {"in": 0.25, "cached": 0.025, "cwrite": 0.25, "out": 2.00},
}
MODEL_WEIGHTS = [("demo-large", 0.55), ("demo-medium", 0.30), ("demo-small", 0.15)]

REPOS = ["acme/web-frontend", "acme/api-gateway", "acme/infra-terraform",
         "acme/mobile-app", "acme/data-pipeline", None]
BRANCHES = ["main", "feature/checkout", "fix/login-redirect", "chore/deps", "feature/search"]
SUMMARIES = [
    "Refactor the checkout flow to use the new pricing API",
    "Fix flaky integration tests in the auth module",
    "Add pagination to the search results endpoint",
    "Investigate slow queries on the reporting dashboard",
    "Migrate CI from self-hosted runners to hosted",
    "Write unit tests for the retry middleware",
    "Upgrade to the latest framework major version",
    "Document the deployment runbook",
]
HOSTS = ["cli", "vscode", "app", "cloud-agent"]
INITIATORS = ["user", "agent"]
EFFORTS = ["low", "medium", "high", None]


def pick_model(rnd: random.Random) -> str:
    x = rnd.random()
    acc = 0.0
    for name, w in MODEL_WEIGHTS:
        acc += w
        if x <= acc:
            return name
    return MODEL_WEIGHTS[-1][0]


def nano_aiu(model: str, tin: int, cread: int, cwrite: int, tout: int) -> int:
    """AIC = (トークン × 単価) / 1e6 × 100。input_tokens は cache を内包する。"""
    p = PRICING[model]
    fresh = max(tin - cread - cwrite, 0)
    usd = (fresh * p["in"] + cread * p["cached"] + cwrite * p["cwrite"] + tout * p["out"]) / 1e6
    return int(usd * 100 * 1e9)


def busy_factor(ts: datetime, rnd: random.Random) -> float:
    """平日の日中に山が来るような、それらしい濃淡を作る。"""
    f = 1.0
    if ts.weekday() >= 5:
        f *= 0.25
    hour = ts.hour
    if 9 <= hour < 19:
        f *= 1.0
    elif 19 <= hour < 23 or 7 <= hour < 9:
        f *= 0.45
    else:
        f *= 0.05
    return f * rnd.uniform(0.4, 1.6)


def generate(out: Path, days: int, seed: int, sessions_per_day: int) -> dict:
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.exists():
        out.unlink()
    for suffix in ("-wal", "-shm"):
        p = Path(str(out) + suffix)
        if p.exists():
            p.unlink()

    rnd = random.Random(seed)
    con = sqlite3.connect(out.as_posix())
    con.executescript(SCHEMA)

    now = datetime.now(timezone.utc).replace(microsecond=0)
    start = now - timedelta(days=days)

    sessions, events = [], []
    for day in range(days):
        day_start = start + timedelta(days=day)
        n_sessions = max(1, int(rnd.gauss(sessions_per_day, sessions_per_day * 0.4)))
        # たまに「集中的に回した日」を作る（レート制限の議論に効く山）
        if rnd.random() < 0.12:
            n_sessions *= 3

        for _ in range(n_sessions):
            sid = str(uuid.uuid4())
            hour = rnd.choices(range(24), weights=[busy_factor(
                day_start.replace(hour=h), rnd) for h in range(24)])[0]
            s_start = day_start.replace(hour=hour, minute=rnd.randrange(60))
            if s_start > now:
                continue
            repo = rnd.choice(REPOS)
            sessions.append((
                sid,
                f"/home/dev/{repo.split('/')[-1]}" if repo else "/home/dev/scratch",
                repo,
                rnd.choice(BRANCHES) if repo else None,
                rnd.choice(SUMMARIES),
                s_start.isoformat().replace("+00:00", "Z"),
                s_start.isoformat().replace("+00:00", "Z"),
                rnd.choice(HOSTS),
            ))

            ts = s_start
            for turn in range(rnd.randrange(2, 26)):
                ts += timedelta(seconds=rnd.randrange(20, 400))
                if ts > now:
                    break
                model = pick_model(rnd)
                cread = rnd.randrange(0, 180_000)
                cwrite = rnd.randrange(0, 20_000)
                tin = cread + cwrite + rnd.randrange(500, 30_000)
                tout = rnd.randrange(50, 6_000)
                reason = int(tout * rnd.uniform(0, 0.6))
                dur = rnd.randrange(1_500, 90_000)
                is_child = rnd.random() < 0.18
                events.append((
                    sid, turn,
                    f"agent-{rnd.randrange(1, 4)}" if is_child else None,
                    str(uuid.uuid4()) if is_child else None,
                    model, tin, tout, cread, cwrite, reason,
                    nano_aiu(model, tin, cread, cwrite, tout),
                    1.0, dur,
                    rnd.randrange(200, 4_000), rnd.randrange(5, 60),
                    rnd.choice(INITIATORS), "/chat/completions",
                    rnd.choice(EFFORTS), "stop", 0, None,
                    ts.isoformat().replace("+00:00", "Z"),
                ))

    con.executemany("INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?)", sessions)
    con.executemany(
        """INSERT INTO assistant_usage_events
           (session_id, turn_index, agent_id, parent_tool_call_id, model,
            input_tokens, output_tokens, cache_read_tokens, cache_write_tokens,
            reasoning_tokens, total_nano_aiu, request_multiplier, duration_ms,
            time_to_first_token_ms, inter_token_latency_ms, initiator, api_endpoint,
            reasoning_effort, finish_reason, content_filter_triggered,
            token_details_json, created_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        events,
    )
    con.commit()
    total = con.execute("SELECT COALESCE(SUM(total_nano_aiu),0) FROM assistant_usage_events").fetchone()[0]
    con.close()
    return {"sessions": len(sessions), "events": len(events), "aic": total / 1e9}


def main() -> int:
    ap = argparse.ArgumentParser(description="デモ用のダミー session-store.db を作る")
    ap.add_argument("--out", type=Path, default=Path("sample/session-store.db"))
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--sessions-per-day", type=int, default=6)
    ap.add_argument("--seed", type=int, default=42)
    args = ap.parse_args()

    st = generate(args.out, args.days, args.seed, args.sessions_per_day)
    print(f"[ok] {args.out}")
    print(f"     {st['sessions']:,} セッション / {st['events']:,} イベント / 合計 {st['aic']:,.1f} AIC")
    print()
    print("次のように使う:")
    print(f"  python aic_collect.py --source {args.out} --archive sample/archive.db")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
