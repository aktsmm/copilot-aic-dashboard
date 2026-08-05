#!/usr/bin/env python3
"""AIU -> AIC 換算の検証スクリプト。

GitHub 公式のモデル別トークン単価から AIC を再計算し、アーカイブ DB の
total_nano_aiu と突合する。一致すれば「1 AIU = 1 AI Credit = $0.01」が
実測で裏付けられる。

ライブ DB は参照しない（読み取り経路は aic_archive に一本化している）。

公式単価: https://docs.github.com/en/copilot/reference/copilot-billing/models-and-pricing

課金式（実測で確定）:
    AIC = ( (input_tokens - cache_read - cache_write) * input単価
          + cache_read  * cached_input単価
          + cache_write * cache_write単価
          + output_tokens * output単価 ) / 1e6 * 100

    ※ input_tokens は cache_read / cache_write を内包している。
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

import aic_archive

HERE = Path(__file__).resolve().parent
TOLERANCE = 0.02

# (長コンテキスト閾値, 閾値以下単価, 閾値超単価)
# 単価は (input, cached_input, cache_write, output) の USD / 1M tokens
PRICING = {
    "gpt-5.6-sol":            (272_000, (5.00, 0.50, 6.25, 30.00), (10.00, 1.00, 12.50, 45.00)),
    "gpt-5.6-terra":          (272_000, (2.00, 0.20, 2.50, 12.00), (4.00, 0.40, 5.00, 18.00)),
    "gpt-5.6-luna":           (200_000, (0.20, 0.02, 0.25, 1.20), (0.40, 0.04, 0.50, 1.80)),
    "gpt-5.5":                (272_000, (5.00, 0.50, None, 30.00), (10.00, 1.00, None, 45.00)),
    "gpt-5.4":                (272_000, (2.50, 0.25, None, 15.00), (5.00, 0.50, None, 22.50)),
    "gpt-5.4-mini":           (None, (0.75, 0.075, None, 4.50), None),
    "gpt-5.3-codex":          (None, (1.75, 0.175, None, 14.00), None),
    "gpt-5-mini":             (None, (0.25, 0.025, None, 2.00), None),
    "claude-opus-5":          (None, (5.00, 0.50, 6.25, 25.00), None),
    "claude-opus-4.8":        (None, (5.00, 0.50, 6.25, 25.00), None),
    "claude-opus-4.6":        (None, (5.00, 0.50, 6.25, 25.00), None),
    "claude-sonnet-5":        (None, (2.00, 0.20, 2.50, 10.00), None),
    "claude-sonnet-4.6":      (None, (3.00, 0.30, 3.75, 15.00), None),
    "claude-haiku-4.5":       (None, (1.00, 0.10, 1.25, 5.00), None),
    "gemini-3.1-pro-preview": (200_000, (2.00, 0.20, None, 12.00), (4.00, 0.40, None, 18.00)),
    "gemini-3.5-flash":       (None, (1.50, 0.15, None, 9.00), None),
    "gemini-3.6-flash":       (None, (1.50, 0.15, None, 7.50), None),
    "mai-code-1-flash":       (None, (0.75, 0.075, None, 4.50), None),
    "grok-4.5":               (200_000, (2.00, 0.50, None, 6.00), None),
}

QUERY = """
SELECT substr(created_at, 1, 10) AS day, model, input_tokens, output_tokens,
       cache_read_tokens, cache_write_tokens, total_nano_aiu
FROM usage_events
WHERE total_nano_aiu IS NOT NULL AND total_nano_aiu > 0
ORDER BY created_at
"""


def price_aic(model: str, in_tok: int, out_tok: int, cache_r: int, cache_w: int):
    entry = PRICING.get(model)
    if entry is None:
        return None
    threshold, low, high = entry
    rate = high if (threshold and high and in_tok > threshold) else low
    p_in, p_cached, p_write, p_out = rate
    billable_in = in_tok - cache_r - cache_w
    if billable_in < 0:
        return None
    usd = (
        billable_in / 1e6 * p_in
        + cache_r / 1e6 * p_cached
        + (cache_w / 1e6 * p_write if p_write else 0.0)
        + out_tok / 1e6 * p_out
    )
    return usd * 100


def main() -> int:
    cfg = aic_archive.load_config(HERE)
    ap = argparse.ArgumentParser(description="AIU -> AIC 換算をアーカイブ上で検証する")
    ap.add_argument("--archive", default=cfg["archive_db"], help="アーカイブ DB のパス")
    args = ap.parse_args()

    # ライブ DB ではなくアーカイブを読む。ライブ DB を触る経路を 1 本に絞ることで、
    # db/-wal/-shm のコピーのような危険な読み方が紛れ込む余地をなくす。
    archive_path = Path(args.archive)
    if not archive_path.exists():
        print(f"[error] アーカイブ DB がありません: {archive_path}", file=sys.stderr)
        print("        先に python aic_archive.py を実行してください。", file=sys.stderr)
        return 1

    conn = sqlite3.connect(f"file:{archive_path.as_posix()}?mode=ro", uri=True, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(QUERY).fetchall()
    finally:
        conn.close()

    stats = defaultdict(lambda: {"n": 0, "ok": 0, "actual": 0.0, "calc": 0.0})
    drift = defaultdict(lambda: defaultdict(lambda: [0, 0.0, 0.0]))
    skipped = 0

    for r in rows:
        model = r["model"]
        actual = r["total_nano_aiu"] / 1e9
        calc = price_aic(model, r["input_tokens"] or 0, r["output_tokens"] or 0,
                         r["cache_read_tokens"] or 0, r["cache_write_tokens"] or 0)
        if calc is None:
            skipped += 1
            continue
        s = stats[model]
        s["n"] += 1
        s["actual"] += actual
        s["calc"] += calc
        if actual > 0 and abs(calc - actual) / actual < TOLERANCE:
            s["ok"] += 1
        d = drift[model][r["day"]]
        d[0] += 1
        d[1] += calc
        d[2] += actual

    tot_n = sum(s["n"] for s in stats.values())
    tot_ok = sum(s["ok"] for s in stats.values())
    tot_a = sum(s["actual"] for s in stats.values())
    tot_c = sum(s["calc"] for s in stats.values())

    if tot_n == 0 or tot_a <= 0:
        print("[warn] 突合できる行がありません。", file=sys.stderr)
        print(f"       アーカイブ {len(rows):,} 行のうち、単価表に無いモデルが {skipped:,} 行。", file=sys.stderr)
        print("       PRICING テーブルにモデルを追加するか、しばらく使ってから再実行してください。", file=sys.stderr)
        return 1

    print("=" * 78)
    print("AIU -> AIC 換算検証  (公式トークン単価から再計算し total_nano_aiu と突合)")
    print("=" * 78)
    print(f"対象 {tot_n:,} 件 (単価未登録モデル {skipped} 件スキップ)")
    print(f"誤差 {TOLERANCE:.0%} 以内で一致: {tot_ok:,} / {tot_n:,} = {tot_ok / tot_n * 100:.1f}%")
    print(f"アーカイブ合計 {tot_a:,.1f} AIC   公式単価での再計算 {tot_c:,.1f} AIC "
          f"(差 {(tot_c - tot_a) / tot_a * 100:+.2f}%)\n")

    print(f"{'model':24}{'件数':>7}{'一致率':>9}{'DB AIC':>13}{'計算 AIC':>13}{'差':>9}")
    print("-" * 78)
    mismatched = []
    for model, s in sorted(stats.items(), key=lambda kv: -kv[1]["actual"]):
        rate = s["ok"] / s["n"] * 100
        diff = (s["calc"] - s["actual"]) / s["actual"] * 100 if s["actual"] else 0
        print(f"{model:24}{s['n']:7}{rate:8.1f}%{s['actual']:13,.1f}{s['calc']:13,.1f}{diff:+8.1f}%")
        if rate < 90:
            mismatched.append(model)

    # 一致率が低いモデルは、期間中に単価改定があった可能性を日別に確認する
    for model in mismatched:
        print(f"\n--- {model}: 日別の 実測/計算 比率（1.000 なら現行の公式単価と一致）---")
        print(f"{'date':12}{'件数':>7}{'計算 AIC':>12}{'実測 AIC':>12}{'比率':>9}")
        for day in sorted(drift[model]):
            n, c, a = drift[model][day]
            print(f"{day:12}{n:7}{c:12,.0f}{a:12,.0f}{(a / c if c else 0):9.3f}")
        print("  ※ 比率が日付で階段状に変わる場合は、その時期に単価改定があったことを示す。")

    print("\n【結論】")
    print("  ・課金式: AIC = ((input - cache_read - cache_write) * input単価")
    print("                  + cache_read * cached単価 + cache_write * cache_write単価")
    print("                  + output * output単価) / 1e6 * 100")
    print("  ・input_tokens は cache_read / cache_write を内包する。")
    print("  ・total_nano_aiu / 1e9 は AI Credits そのもの。1 AIC = $0.01。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
