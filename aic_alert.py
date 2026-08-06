#!/usr/bin/env python3
"""消費が閾値を超えたら通知する。

なぜ「事後検知」ではなく「事前警告」なのか:
GitHub はレート制限の数値もウィンドウ幅も公開しておらず、ローカル DB にも
スロットリングの記録は一切残らない（429 やクォータのイベント欄自体が無い）。
つまり「制限を踏んだ瞬間」を後から特定する手段が無い。できるのは自分の
消費カーブを見て手前で気づくことだけなので、閾値超過を能動的に知らせる。

依存は標準ライブラリのみ。通知は Windows 標準の機構だけを使う。
"""

from __future__ import annotations

import json
import math
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

# 通知の種類。(meta キー, 表示名)
KIND_HOUR = "hourly"
KIND_DAY = "rolling24h"
KIND_MONTH = "monthly"


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _sum_since(arc, since: datetime, factor: float = 1.0) -> float:
    row = arc.execute(
        "SELECT COALESCE(SUM(total_nano_aiu), 0) / 1e9 FROM usage_events WHERE created_at >= ?",
        (_iso(since),),
    ).fetchone()
    return float(row[0] or 0.0) * factor


def _month_to_date(arc, now: datetime, tz_offset_hours: float, factor: float = 1.0) -> tuple:
    """課金は暦月で締まるので、ローカル時刻の月初からの累計を返す。"""
    tz = timezone(timedelta(hours=tz_offset_hours))
    local_now = now.astimezone(tz)
    start_local = local_now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    start_utc = start_local.astimezone(timezone.utc)
    return _sum_since(arc, start_utc, factor), local_now.strftime("%Y-%m")


def _meta_get(arc, key: str):
    row = arc.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row[0] if row else None


def _meta_set(arc, key: str, value: str) -> None:
    arc.execute(
        "INSERT INTO meta(key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def _tier(value: float, threshold: float) -> int:
    """超過の程度を 2 倍刻みの段位で表す。

    同じ期間に鳴り続けるのは邪魔だが、桁が変わるほど悪化したなら
    もう一度知らせたい。そこで「倍になったら段位が上がる」形にする。

    比を取ってから log2 すると、閾値が極端に小さいときに inf になって
    OverflowError で評価全体が落ちる。差で計算して落ちないようにする。
    """
    if threshold <= 0 or value <= threshold:
        return 0
    try:
        t = math.log2(value) - math.log2(threshold)
    except ValueError:
        return 0
    if not math.isfinite(t):
        return 0
    return max(0, min(int(t), 60))


def evaluate(arc, cfg: dict, now: datetime | None = None) -> list:
    """閾値を超えている項目を返す。副作用なし（発火記録は fire() 側）。

    同じ状況で鳴り続けないよう、種類ごとに「期間」と「段位」を持たせる。
    通知するのは、期間が変わったとき、または段位が上がったときだけ。
      直近1時間  … 期間 = 時（レート制限に効くので短め）
      直近24時間 … 期間 = ローカル日
      今月累計   … 期間 = ローカル日
    段位は超過が倍になるたびに 1 上がる。下がっただけでは鳴らさない
    （fire() が期間内の最高段位を覚えているため、行ったり来たりしない）。

    now を外から渡せるのは、3 つの判定を同じ時刻で行うため。月末や
    時刻の境界で評価ごとに now を取り直すと、期間がずれることがある。
    """
    tz_hours = float(cfg.get("tz_offset_hours", 0) or 0)
    tz = timezone(timedelta(hours=tz_hours))
    now = now or _now_utc()
    today_local = now.astimezone(tz).strftime("%Y-%m-%d")
    # ダッシュボードの KPI と単位を必ず揃える。片方だけ換算していると
    # 「グラフでは超えているのに通知が来ない」という最悪の食い違いになる。
    factor = float(cfg.get("aiu_to_aic", 1.0) or 1.0)
    alerts = []

    hourly_thr = float(cfg.get("hourly_alert_aic") or 0)
    if hourly_thr > 0:
        v = _sum_since(arc, now - timedelta(hours=1), factor)
        if v > hourly_thr:
            alerts.append({
                "kind": KIND_HOUR,
                "period": now.strftime("%Y-%m-%dT%H"),
                "tier": _tier(v, hourly_thr),
                "value": v, "threshold": hourly_thr,
                "title": "Copilot: 直近1時間の消費が多いです",
                "body": f"直近1時間 {v:,.0f} AIC（閾値 {hourly_thr:,.0f}）",
            })

    daily_thr = float(cfg.get("daily_budget_aic") or 0)
    if daily_thr > 0:
        v = _sum_since(arc, now - timedelta(hours=24), factor)
        if v > daily_thr:
            alerts.append({
                "kind": KIND_DAY,
                "period": today_local,
                "tier": _tier(v, daily_thr),
                "value": v, "threshold": daily_thr,
                "title": "Copilot: 直近24時間の消費が多いです",
                "body": f"直近24時間 {v:,.0f} AIC（閾値 {daily_thr:,.0f}）",
            })

    included = float(cfg.get("monthly_included_aic") or 0)
    ratio = float(cfg.get("monthly_alert_ratio") or 0.8)
    if included > 0 and ratio > 0:
        v, ym = _month_to_date(arc, now, tz_hours, factor)
        thr = included * ratio
        if v > thr:
            pct = v / included * 100
            alerts.append({
                "kind": KIND_MONTH,
                "period": today_local,
                "tier": _tier(v, thr),
                "value": v, "threshold": thr,
                "title": "Copilot: 今月の含有クレジットを消化しています",
                "body": f"{ym} 累計 {v:,.0f} / {included:,.0f} AIC（{pct:.0f}%）",
            })

    return alerts


def _powershell() -> str | None:
    for exe in ("pwsh.exe", "powershell.exe"):
        try:
            subprocess.run([exe, "-NoProfile", "-Command", "exit 0"],
                           capture_output=True, timeout=20, check=True)
            return exe
        except (OSError, subprocess.SubprocessError):
            continue
    return None


# 値は環境変数で渡す。-Command に渡したスクリプトは param() に束縛されず、
# 引数を文字列に埋め込むとクォートとインジェクションの問題も抱えるため。
_TOAST_PS = r"""
$ErrorActionPreference = 'Stop'
$Title = $env:AIC_TOAST_TITLE
$Body  = $env:AIC_TOAST_BODY
try {
    # WinRT のトースト。追加モジュール不要で Windows 標準。
    [void][Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType=WindowsRuntime]
    [void][Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom, ContentType=WindowsRuntime]
    $tpl = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent(
        [Windows.UI.Notifications.ToastTemplateType]::ToastText02)
    $texts = $tpl.GetElementsByTagName('text')
    $texts.Item(0).AppendChild($tpl.CreateTextNode($Title)) | Out-Null
    $texts.Item(1).AppendChild($tpl.CreateTextNode($Body))  | Out-Null
    # 自前の AppUserModelID は登録が要るので、PowerShell 自身のものを借りる。
    $aumid = '{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe'
    $toast = [Windows.UI.Notifications.ToastNotification]::new($tpl)
    [Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($aumid).Show($toast)
    exit 0
} catch {
    try {
        # トーストが使えない環境向けのバルーン通知。
        Add-Type -AssemblyName System.Windows.Forms
        Add-Type -AssemblyName System.Drawing
        $ni = New-Object System.Windows.Forms.NotifyIcon
        $ni.Icon = [System.Drawing.SystemIcons]::Warning
        $ni.Visible = $true
        $ni.ShowBalloonTip(10000, $Title, $Body, [System.Windows.Forms.ToolTipIcon]::Warning)
        Start-Sleep -Seconds 6
        $ni.Dispose()
        exit 0
    } catch { exit 1 }
}
"""


def notify(title: str, body: str) -> bool:
    """OS の通知を出す。出せなければ False（呼び出し側は標準出力で代替）。"""
    if sys.platform != "win32":
        return False
    shell = _powershell()
    if not shell:
        return False
    env = dict(os.environ, AIC_TOAST_TITLE=title, AIC_TOAST_BODY=body)
    try:
        r = subprocess.run(
            [shell, "-NoProfile", "-NonInteractive", "-Command", _TOAST_PS],
            capture_output=True, timeout=60, env=env,
        )
        return r.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def fire(arc, cfg: dict, quiet: bool = False) -> list:
    """閾値評価 → 未通知のものだけ通知 → 発火を記録。

    meta への書き込みだけを行う。usage_events など追記専用のテーブルには触らない。

    通知に失敗したら状態を進めない。進めてしまうと「1 回鳴らそうとして
    失敗した」だけで以後ずっと黙ることになり、利用者は静かなことを
    「問題なし」と受け取る。これがこのツールで最も避けたい壊れ方。
    """
    if not cfg.get("alerts_enabled", True):
        return []

    sent = []
    now = _now_utc()
    for a in evaluate(arc, cfg, now):
        key = f"alert_state:{a['kind']}"
        prev = _meta_get(arc, key) or ""
        prev_period, _, prev_tier = prev.partition("|")
        same_period = prev_period == a["period"]
        if same_period:
            try:
                if a["tier"] <= int(prev_tier or 0):
                    continue      # 同じ期間で、まだ悪化していない
            except ValueError:
                pass

        ok = notify(a["title"], a["body"])
        a["notified"] = ok
        if ok:
            # 期間内は最高段位だけを覚える。下がってから戻ったときに
            # 鳴り直さないようにするため。
            keep = a["tier"]
            if same_period:
                try:
                    keep = max(keep, int(prev_tier or 0))
                except ValueError:
                    pass
            _meta_set(arc, key, f"{a['period']}|{keep}")
            _meta_set(arc, f"alert_last_fired:{a['kind']}", _iso(now))
            _meta_set(arc, f"alert_delivery_failed:{a['kind']}", "")
        else:
            # 未達として残し、次回も再試行させる。ダッシュボードにも出す。
            _meta_set(arc, f"alert_delivery_failed:{a['kind']}", _iso(now))
        sent.append(a)
        if not quiet or not ok:
            # 通知に失敗した場合は握り潰さず必ず標準出力に出す。
            mark = "" if ok else "（通知の表示に失敗したため標準出力に出します）"
            print(f"[alert] {a['title']} — {a['body']}{mark}")
    arc.commit()
    return sent


def delivery_failures(arc) -> list:
    """通知が届かなかった種類。ダッシュボードで表に出すために使う。"""
    rows = arc.execute(
        "SELECT key, value FROM meta WHERE key LIKE 'alert_delivery_failed:%' AND value <> ''"
    ).fetchall()
    return [{"kind": k.split(":", 1)[1], "at": v} for k, v in rows]


def main() -> int:
    import argparse
    import aic_archive

    ap = argparse.ArgumentParser(description="消費が閾値を超えていたら通知する")
    ap.add_argument("--check", action="store_true",
                    help="通知を出さず、閾値の評価結果だけ表示する")
    ap.add_argument("--test", action="store_true",
                    help="通知が実際に表示されるかだけ試す")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    if args.test:
        ok = notify("Copilot AIC ダッシュボード", "通知のテストです。これが見えていれば設定は正常です。")
        print("[ok] 通知を表示しました。" if ok else
              "[warn] 通知を表示できませんでした。標準出力での警告に切り替わります。")
        return 0 if ok else 1

    here = Path(__file__).resolve().parent
    cfg = aic_archive.load_config(here)
    arc_path = Path(cfg["archive_db"])
    if not arc_path.exists():
        print(f"[skip] アーカイブがありません: {arc_path}", file=sys.stderr)
        return 0

    arc = aic_archive.open_archive(arc_path)
    try:
        if args.check:
            found = evaluate(arc, cfg)
            if not found:
                print("[ok] 閾値を超えている項目はありません。")
            for a in found:
                print(f"[over] {a['kind']:<10} {a['value']:>10,.0f} AIC "
                      f"(閾値 {a['threshold']:,.0f}) / 段位 {a['tier']}")
            for f in delivery_failures(arc):
                print(f"[warn] {f['kind']} の通知が {f['at']} に届いていません。")
            return 0
        # 手動実行と定期実行が重なると、両方が同じ超過を通知しうる。
        # アーカイブと同じロックで直列化する。
        with aic_archive.ProcessLock(arc_path.with_suffix(".alert.lock")):
            fire(arc, cfg, quiet=args.quiet)
    finally:
        arc.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
