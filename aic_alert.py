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


def _parse_ts(ts: str):
    """アーカイブの created_at（2026-07-07T05:53:00.000Z 形式）を datetime に。"""
    if not ts:
        return None
    s = str(ts).strip().replace("Z", "+00:00").replace(" ", "T")
    try:
        d = datetime.fromisoformat(s)
    except ValueError:
        try:
            d = datetime.strptime(str(ts)[:19], "%Y-%m-%dT%H:%M:%S")
        except (ValueError, TypeError):
            return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def _percentile(sorted_vals: list, pct: float) -> float:
    """nearest-rank のパーセンタイル。補間しない。

    補間するとサンプルが少ないときに「どのデータ点でもない値」が出て、
    なぜその閾値になったのか説明できなくなる。ここは説明可能性を優先する。
    """
    if not sorted_vals:
        return 0.0
    k = math.ceil(pct / 100.0 * len(sorted_vals))
    return float(sorted_vals[max(1, min(k, len(sorted_vals))) - 1])


def _rolling_values(arc, now: datetime, days: float, window_h: float,
                    step_min: int, factor: float) -> tuple:
    """過去 days 日の「直近 window_h 時間の消費」を step_min 刻みで並べた分布。

    通知が測るのと同じ量（移動窓の合計）で分布を作る。時バケットの分布で
    代用すると、移動窓の方が上振れしやすいぶん想定より多く鳴ってしまう。

    現在進行中の窓は基準に入れない。入れると、いま起きているスパイクが
    自分の閾値を押し上げ、大きく使ったときほど鳴らなくなる。

    ゼロの窓は除外する。全体の 8 割は消費ゼロなので、混ぜると分布が
    ゼロ付近に沈み、稼働中はほぼ常時超過という使えない閾値になる。
    """
    window = timedelta(hours=window_h)
    end = now - window                   # 進行中の窓を基準から外す
    start = now - timedelta(days=days)
    if end <= start:
        return [], 0.0

    rows = arc.execute(
        "SELECT created_at, total_nano_aiu FROM usage_events "
        "WHERE created_at >= ? AND created_at < ? ORDER BY created_at",
        (_iso(start - window), _iso(end)),
    ).fetchall()

    times, prefix = [], [0.0]
    for ts, nano in rows:
        t = _parse_ts(ts)
        if t is None:
            continue
        times.append(t)
        prefix.append(prefix[-1] + float(nano or 0) / 1e9 * factor)
    if not times:
        return [], 0.0

    step = timedelta(minutes=step_min)
    vals, i, j, t = [], 0, 0, start
    n = len(times)
    while t <= end:
        lo = t - window
        while j < n and times[j] <= t:
            j += 1
        while i < j and times[i] <= lo:
            i += 1
        v = prefix[j] - prefix[i]
        if v > 0:
            vals.append(v)
        t += step

    vals.sort()
    return vals, (times[-1] - times[0]).total_seconds() / 86400.0


# 種類ごとの測り方。(窓の長さ[h], 標本刻み[分], パーセンタイル設定キー, 表示名)
_BASELINE_SPEC = {
    KIND_HOUR: (1.0, 15, "hourly_baseline_percentile", "直近1時間"),
    KIND_DAY: (24.0, 60, "daily_baseline_percentile", "直近24時間"),
}


def baseline(arc, cfg: dict, now: datetime, factor: float) -> dict:
    """1時間 / 24時間それぞれの「普段の上位 N%」を返す。

    データが足りないうちは基準を使わない（enough=False）。導入直後の
    数時間分から出した閾値は、その人の普段を表していないため。
    """
    out = {KIND_HOUR: None, KIND_DAY: None}
    if not cfg.get("baseline_enabled", True):
        return out
    days = float(cfg.get("baseline_days", 30) or 30)
    min_days = float(cfg.get("baseline_min_days", 7) or 7)
    min_samples = int(cfg.get("baseline_min_samples", 40) or 40)

    for kind, (window_h, step, key, _label) in _BASELINE_SPEC.items():
        pct = float(cfg.get(key) or 0)
        if pct <= 0:
            continue
        vals, span = _rolling_values(arc, now, days, window_h, step, factor)
        out[kind] = {
            "threshold": _percentile(vals, pct),
            "samples": len(vals), "days": span, "percentile": pct,
            "enough": span >= min_days and len(vals) >= min_samples,
        }
    return out


def _threshold(fixed: float, b: dict | None) -> tuple:
    """実際に使う閾値と、その根拠を返す。

    固定値は「下限」として効かせる。基準だけにすると、使い始めや軽い
    使い方の人で閾値が数十 AIC まで下がり、些細な作業でも鳴ってしまう。
    逆に基準が固定値を上回るときは基準を使う。そうしないと、よく使う人ほど
    鳴りっぱなしになって通知そのものを無視するようになる。
    """
    if b and b.get("enough") and b.get("threshold", 0) > 0:
        if b["threshold"] >= fixed:
            return b["threshold"], f"直近{b['days']:.0f}日の p{b['percentile']:.0f}"
        return fixed, f"下限（基準 p{b['percentile']:.0f} は {b['threshold']:,.0f}）"
    if b and not b.get("enough") and b.get("samples", 0) >= 0:
        return fixed, "固定値（基準を出すにはデータ不足）"
    return fixed, "固定値"


def _meta_set(arc, key: str, value: str) -> None:
    arc.execute(
        "INSERT INTO meta(key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


# 期間内に鳴らし直す条件。前回知らせた「消費量」がこの倍率まで伸びたとき。
# 閾値ではなく消費量を基準にするのは、閾値が実行ごとに動くため。
_ESCALATE = 2.0


def _tier(value: float, threshold: float) -> int:
    """超過の程度を 2 倍刻みの段位で表す（表示専用）。

    再通知の判定には使わない。閾値は毎回の実行で基準から計算し直すため
    実行ごとに動き、同じ消費量でも段位が変わってしまうため。
    詳しくは fire() の説明を参照。

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

    同じ状況で鳴り続けないよう、種類ごとに「期間」を持たせる。
      直近1時間  … 期間 = 時（レート制限に効くので短め）
      直近24時間 … 期間 = ローカル日
      今月累計   … 期間 = ローカル日
    期間内に鳴らし直すかどうかは fire() が消費量の伸びで判定する。

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
    base = baseline(arc, cfg, now, factor)
    alerts = []

    hourly_thr, hourly_why = _threshold(float(cfg.get("hourly_alert_aic") or 0),
                                        base.get(KIND_HOUR))
    if hourly_thr > 0:
        v = _sum_since(arc, now - timedelta(hours=1), factor)
        if v > hourly_thr:
            alerts.append({
                "kind": KIND_HOUR,
                "period": now.strftime("%Y-%m-%dT%H"),
                "tier": _tier(v, hourly_thr),
                "value": v, "threshold": hourly_thr, "basis": hourly_why,
                "title": "Copilot: 直近1時間の消費が多いです",
                "body": f"直近1時間 {v:,.0f} AIC（閾値 {hourly_thr:,.0f} / {hourly_why}）",
            })

    daily_thr, daily_why = _threshold(float(cfg.get("daily_budget_aic") or 0),
                                      base.get(KIND_DAY))
    if daily_thr > 0:
        v = _sum_since(arc, now - timedelta(hours=24), factor)
        if v > daily_thr:
            alerts.append({
                "kind": KIND_DAY,
                "period": today_local,
                "tier": _tier(v, daily_thr),
                "value": v, "threshold": daily_thr, "basis": daily_why,
                "title": "Copilot: 直近24時間の消費が多いです",
                "body": f"直近24時間 {v:,.0f} AIC（閾値 {daily_thr:,.0f} / {daily_why}）",
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
                "basis": "含有クレジットの割合（実際の請求に対応するので相対化しない）",
                "title": "Copilot: 今月の含有クレジットを消化しています",
                "body": f"{ym} 累計 {v:,.0f} / {included:,.0f} AIC（{pct:.0f}%）",
            })

    return alerts


# PowerShell を呼ぶときにコンソール窓を出さない。定期実行で窓が開くと、
# 数時間おきに作業中のフォーカスを奪われる。通知そのものが邪魔になっては
# 本末転倒なので、子プロセスは必ず窓なしで起動する。
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0


def _powershell() -> str | None:
    for exe in ("pwsh.exe", "powershell.exe"):
        try:
            subprocess.run([exe, "-NoProfile", "-Command", "exit 0"],
                           capture_output=True, timeout=20, check=True,
                           creationflags=_NO_WINDOW)
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
            creationflags=_NO_WINDOW,
        )
        return r.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def fire(arc, cfg: dict, quiet: bool = False) -> list:
    """閾値評価 → 未通知のものだけ通知 → 発火を記録。

    meta への書き込みだけを行う。usage_events など追記専用のテーブルには触らない。

    期間内に鳴らし直す条件は「前回知らせたときより消費量が _ESCALATE 倍に
    伸びたか」。閾値との比（段位）では判定しない。閾値は毎回の実行で基準から
    計算し直すため実行ごとに動き、次の 2 つの壊れ方をするため:
      - 大きく使っている最中は、その消費自体が基準を押し上げて閾値が上がる。
        消費が倍になっても段位は据え置きか下がり、いちばん知らせるべき
        ときに黙る。
      - 逆に基準が下がると、消費が同じでも段位だけ上がり「悪化した」と
        誤って鳴る。
    消費量そのものを覚えておけば、閾値がどう動いても「倍に増えた」は
    「倍に増えた」のままになる。

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
        prev_period, _, prev_mark = prev.partition("|")
        same_period = prev_period == a["period"]
        prev_value = None
        if same_period:
            if prev_mark.startswith("v"):
                try:
                    prev_value = float(prev_mark[1:])
                except ValueError:
                    prev_value = None
            elif prev_mark:
                # 段位を記録していた旧形式。消費量に読み替えられないので、
                # この期間はもう鳴らさない（誤って鳴らすより黙る方を選ぶ）。
                continue
            if prev_value is not None and a["value"] < prev_value * _ESCALATE:
                continue      # 同じ期間で、まだ倍には増えていない

        ok = notify(a["title"], a["body"])
        a["notified"] = ok
        if ok:
            # 知らせた時点の消費量を覚える。次はここから倍に増えたときだけ。
            _meta_set(arc, key, f"{a['period']}|v{a['value']:.6g}")
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


def tune(arc, cfg: dict, days: float, candidates: tuple) -> dict:
    """過去を再生して「その設定なら何回鳴っていたか」を実測する。

    パーセンタイルから通知頻度を暗算で出すことはできない。移動窓の超過は
    数時間にわたって続き、複数の期間にまたがって鳴る。さらに基準は過去
    30 日の分布なので、使用量が増えている時期には実際の超過率が名目の
    (100 - p)% を大きく上回る。だから机上ではなく実データで測る。

    基準は再生する各時点で作り直す。その時点までに閉じた窓しか使わないので
    後知恵は入らない。標本の格子は now を起点に固定しているため、朝に流しても
    夜に流しても同じ履歴なら同じ回数になる。
    """
    factor = float(cfg.get("aiu_to_aic", 1.0) or 1.0)
    now = _now_utc()
    base_days = float(cfg.get("baseline_days", 30) or 30)
    min_days = float(cfg.get("baseline_min_days", 7) or 7)
    min_samples = int(cfg.get("baseline_min_samples", 40) or 40)

    # 全区間を一度だけ読み、累積和で窓合計を O(1) にする。
    rows = arc.execute(
        "SELECT created_at, total_nano_aiu FROM usage_events ORDER BY created_at"
    ).fetchall()
    times, prefix = [], [0.0]
    for ts, nano in rows:
        t = _parse_ts(ts)
        if t is None:
            continue
        times.append(t)
        prefix.append(prefix[-1] + float(nano or 0) / 1e9 * factor)
    if len(times) < 2:
        return {"days": 0.0, "results": {}}

    import bisect
    span_days = (times[-1] - times[0]).total_seconds() / 86400.0
    days = min(days, max(0.0, span_days - min_days))
    if days <= 0:
        return {"days": 0.0, "results": {}, "span": span_days}

    def wsum(a, b):
        return prefix[bisect.bisect_right(times, b)] - prefix[bisect.bisect_right(times, a)]

    results = {}
    for kind, (window_h, bstep, key, label) in _BASELINE_SPEC.items():
        fixed = float(cfg.get("hourly_alert_aic" if kind == KIND_HOUR
                              else "daily_budget_aic") or 0)
        period_fmt = "%Y-%m-%dT%H" if kind == KIND_HOUR else "%Y-%m-%d"
        step = timedelta(minutes=30 if kind == KIND_HOUR else 60)
        window = timedelta(hours=window_h)
        base_span = timedelta(days=base_days)

        # 基準の標本を等間隔の格子の上に一度だけ作る。以降はどの再生時点でも
        # この格子を切り出すだけなので、標本を作り直す必要がない。
        #
        # 格子は now を起点に遡って刻む。以前は 1 日ぶんの基準を使い回して
        # いたが、それだと (a) その日の朝の時点を評価するのに夕方までの窓が
        # 混ざって後知恵になり、(b) 起点が「実行した時刻」なので朝に流すか
        # 夜に流すかで結果が変わっていた。格子を固定すれば両方なくなる。
        gstep = timedelta(minutes=bstep)
        nback = int((now - times[0]).total_seconds() // gstep.total_seconds()) + 2
        gtimes, gvals = [], []
        gt = now - gstep * max(nback, 0)
        while gt <= now:
            gtimes.append(gt)
            gvals.append(wsum(gt - window, gt))
            gt += gstep

        per_pct = {p: [0, None] for p in candidates}   # [鳴った回数, 直前に知らせた量]
        periods = {p: None for p in candidates}
        nfire = int(days * 86400 // step.total_seconds())
        t = now - step * nfire
        while t <= now:
            # その時点までに閉じた窓だけを基準にする。production の
            # _rolling_values(arc, t, ...) と同じ範囲を切り出している。
            lo = bisect.bisect_left(gtimes, t - base_span)
            hi = bisect.bisect_right(gtimes, t - window)
            vals = sorted(v for v in gvals[lo:hi] if v > 0)
            si = bisect.bisect_left(times, t - base_span - window)
            ei = bisect.bisect_left(times, t - window)
            vspan = ((times[ei - 1] - times[si]).total_seconds() / 86400.0
                     if ei > si else 0.0)
            enough = vspan >= min_days and len(vals) >= min_samples
            v = wsum(t - window, t)
            for pct in candidates:
                thr = max(fixed, _percentile(vals, pct)) if enough else fixed
                if thr <= 0 or v <= thr:
                    continue
                p = t.strftime(period_fmt)
                st = per_pct[pct]
                if periods[pct] != p:                 # 期間が変わったので必ず鳴る
                    periods[pct] = p
                    st[0] += 1
                    st[1] = v
                elif st[1] is None or v >= st[1] * _ESCALATE:
                    st[0] += 1                        # 期間内でも倍に増えたら鳴る
                    st[1] = v
            t += step

        span_replayed = nfire * step.total_seconds() / 86400.0 or days
        per_out = {p: {"fires": per_pct[p][0],
                       "per_month": per_pct[p][0] / span_replayed * 30.0}
                   for p in candidates}
        vals_now, _ = _rolling_values(arc, now, base_days, window_h, bstep, factor)
        results[kind] = {"label": label, "by_percentile": per_out,
                         "current": {p: _percentile(vals_now, p) for p in candidates},
                         "config_key": key}
    return {"days": days, "span": span_days, "results": results}


def main() -> int:
    import argparse
    import aic_archive

    ap = argparse.ArgumentParser(description="消費が閾値を超えていたら通知する")
    ap.add_argument("--check", action="store_true",
                    help="通知を出さず、閾値の評価結果だけ表示する")
    ap.add_argument("--tune", action="store_true",
                    help="過去を再生して、パーセンタイルごとの通知頻度を実測する")
    ap.add_argument("--tune-days", type=float, default=14.0,
                    help="--tune で再生する日数（既定 14）")
    ap.add_argument("--target-per-month", type=float, default=8.0,
                    help="--tune が推奨値を選ぶときの目標通知回数（既定 月8回）")
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
        if args.tune:
            cands = (80, 85, 90, 95, 98, 99)
            r = tune(arc, cfg, args.tune_days, cands)
            if not r.get("results"):
                print(f"[skip] 再生できるだけの履歴がありません"
                      f"（現在 {r.get('span', 0):.1f} 日分）。数日ためてから実行してください。")
                return 0
            print(f"直近 {r['days']:.0f} 日を再生して、設定ごとの通知回数を実測しました。")
            print("（基準は各時点までのデータだけで再計算しているので、後知恵は入っていません）")
            for kind, res in r["results"].items():
                print(f"\n【{res['label']}】config: \"{res['config_key']}\"")
                print("   設定    閾値(現在)      実測          推奨")
                best, best_gap = None, None
                for p in cands:
                    pm = res["by_percentile"][p]["per_month"]
                    gap = abs(pm - args.target_per_month)
                    if best_gap is None or gap < best_gap:
                        best, best_gap = p, gap
                for p in cands:
                    pm = res["by_percentile"][p]["per_month"]
                    thr = res["current"][p]
                    mark = "  ← 推奨" if p == best else ""
                    print(f"   p{p:<5} {thr:>9,.0f} AIC   月 {pm:>5.0f} 回{mark}")
                print(f'   → config.local.json に "{res["config_key"]}": {best} '
                      f'（目標 月{args.target_per_month:.0f}回に最も近い）')
            print("\n目標回数を変えるには --target-per-month を指定してください。")
            return 0

        if args.check:
            now = _now_utc()
            factor = float(cfg.get("aiu_to_aic", 1.0) or 1.0)
            base = baseline(arc, cfg, now, factor)
            print("基準（過去の自分の分布から算出）")
            for kind, label in ((KIND_HOUR, "直近1時間"), (KIND_DAY, "直近24時間")):
                b = base.get(kind)
                if not b:
                    print(f"  {label:<10} 基準なし（設定で無効）")
                    continue
                mark = "使用" if b["enough"] else "データ不足のため固定値を使用"
                print(f"  {label:<10} p{b['percentile']:.0f} = {b['threshold']:>9,.0f} AIC "
                      f"/ 標本 {b['samples']:,} 件 / {b['days']:.1f} 日分 … {mark}")
            print()
            found = evaluate(arc, cfg, now)
            if not found:
                print("[ok] 閾値を超えている項目はありません。")
            for a in found:
                print(f"[over] {a['kind']:<10} {a['value']:>10,.0f} AIC "
                      f"(閾値 {a['threshold']:,.0f} / {a.get('basis', '')}) / 段位 {a['tier']}")
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
