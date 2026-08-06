<#
.SYNOPSIS
    GitHub Copilot AIC ダッシュボードの収集・表示・自動化。

.DESCRIPTION
    ライブ DB (~/.copilot/session-store.db) から追記専用アーカイブへ取り込み、
    集計してダッシュボードを開く。

    アーカイブは既定で ~/.copilot-aic/archive.db（config.json の archive_db、
    または環境変数 AIC_ARCHIVE_DB で変更可）に置かれ、
    ローカル DB を消しても履歴は残る。

.EXAMPLE
    .\run-dashboard.ps1
    収集してブラウザで開く。

.EXAMPLE
    .\run-dashboard.ps1 -InstallTask
    1 時間ごとの自動収集をタスクスケジューラに登録する（取りこぼし防止）。

.EXAMPLE
    .\run-dashboard.ps1 -ExportCsv .\export\usage.csv
    アーカイブ全件を CSV に出力する。

.EXAMPLE
    .\run-dashboard.ps1 -Reconcile
    Copilot App の集計 (~/.copilot/data.db) と突き合わせ、収集の取りこぼしを検出する。
#>
[CmdletBinding()]
param(
    [switch]$NoOpen,
    [switch]$Verify,
    [switch]$Stats,
    [switch]$Reconcile,
    [switch]$TestAlert,
    [switch]$CheckAlert,
    [switch]$TuneAlert,
    [double]$TargetAlertsPerMonth = 8,
    [switch]$InstallTask,
    [ValidateRange(1, 1440)][int]$IntervalMinutes = 60,
    [switch]$UninstallTask,
    [switch]$Demo,
    [string]$ExportCsv,
    [string]$BackupTo
)

$ErrorActionPreference = 'Stop'
$here = $PSScriptRoot
$env:PYTHONIOENCODING = 'utf-8'
$taskName = 'CopilotAicCollect'

function Get-PythonExe {
    foreach ($c in 'python', 'python3', 'py') {
        $cmd = Get-Command $c -ErrorAction SilentlyContinue
        if (-not $cmd) { continue }
        # Microsoft Store のエイリアスは実体が無くても Get-Command に出るので、
        # 実際に起動してバージョンが取れたものだけを採用する。
        $ver = & $cmd.Source -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>$null
        if ($LASTEXITCODE -ne 0 -or -not $ver) { continue }
        $ver = "$ver".Trim()
        if ($ver -notmatch '^\d+\.\d+$') { continue }
        if ([version]$ver -lt [version]'3.9') { continue }
        return $cmd.Source
    }
    throw 'Python 3.9 以上が見つかりません。https://www.python.org/downloads/ からインストールし、PATH を通してください。'
}
$python = Get-PythonExe

function Get-PythonwExe {
    # 定期実行には pythonw.exe を使う。python.exe はコンソールアプリなので
    # 起動のたびに黒い窓が開き、作業中のアクティブウィンドウを奪う。
    # 数時間おきに一瞬だけ現れるので原因が分かりにくく、ツールを消したく
    # なる理由としては十分に大きい。
    param([string]$Exe)
    $dir = Split-Path -Parent $Exe
    foreach ($cand in @('pythonw.exe', 'pyw.exe')) {
        $p = Join-Path $dir $cand
        if (Test-Path -LiteralPath $p) { return $p }
    }
    return $Exe        # 見つからなければ従来どおり（動作優先）
}

switch ($true) {

    $Demo {
        # 合成データだけでダッシュボードを体験する。実データには一切触れない。
        $demoDir = Join-Path $here 'docs/demo'
        New-Item -ItemType Directory -Force -Path $demoDir | Out-Null
        Remove-Item (Join-Path $here 'sample/archive.db*') -ErrorAction SilentlyContinue

        & $python (Join-Path $here 'tools/make_sample_db.py') --out (Join-Path $here 'sample/session-store.db')
        if ($LASTEXITCODE -ne 0) { throw 'サンプル DB の生成に失敗しました' }

        & $python (Join-Path $here 'aic_collect.py') `
            --source (Join-Path $here 'sample/session-store.db') `
            --archive (Join-Path $here 'sample/archive.db') `
            --out (Join-Path $demoDir 'data/usage.json') --redact-paths
        if ($LASTEXITCODE -ne 0) { throw 'サンプル集計に失敗しました' }

        Copy-Item (Join-Path $here 'index.html') (Join-Path $demoDir 'index.html') -Force
        Write-Host "[ok] デモを生成しました: $demoDir\index.html" -ForegroundColor Green
        if (-not $NoOpen) { Start-Process (Join-Path $demoDir 'index.html') }
        return
    }

    $InstallTask {
        # 収集だけを行うタスク。ブラウザは開かない。
        # 窓を出さない pythonw.exe で実行する（無ければ python.exe に戻る）。
        $runner = Get-PythonwExe $python
        $action = New-ScheduledTaskAction -Execute $runner `
            -Argument 'aic_collect.py --quiet' -WorkingDirectory $here

        # 公式にサポートされたトリガーを 2 本登録する。
        #  - AtLogOn : ログオン直後に 1 回
        #  - Once + RepetitionInterval : 以後ずっと指定間隔で繰り返す
        # RepetitionDuration は省略する。省略＝無期限が Task Scheduler の仕様で、
        # [TimeSpan]::MaxValue を渡すと極端な期間としてシリアライズされるため使わない。
        $triggers = @(
            (New-ScheduledTaskTrigger -AtLogOn),
            (New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) `
                    -RepetitionInterval (New-TimeSpan -Minutes $IntervalMinutes))
        )

        $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries `
            -DontStopIfGoingOnBatteries -StartWhenAvailable `
            -ExecutionTimeLimit (New-TimeSpan -Minutes 15) `
            -MultipleInstances IgnoreNew

        try {
            Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $triggers `
                -Settings $settings -Description 'Copilot AIC 使用量をアーカイブへ収集する' -Force -ErrorAction Stop | Out-Null
        }
        catch {
            # Register-ScheduledTask は CIM プロバイダ経由のため、
            # 管理下の端末ではポリシーで拒否されることがある。
            # schtasks.exe は別経路で、非管理者でも通る場合が多いのでフォールバックする。
            Write-Host "[warn] Register-ScheduledTask が失敗: $($_.Exception.Message)" -ForegroundColor Yellow
            Write-Host '       schtasks.exe で再試行します...'

            $script = Join-Path $here 'aic_collect.py'
            # schtasks は分単位と時間単位で書式が違う。60 分未満を HOURLY に
            # 丸めると、指定より粗い間隔で静かに登録されてしまう。
            if ($IntervalMinutes -lt 60) {
                $sc, $mo = 'MINUTE', $IntervalMinutes
            }
            else {
                $sc, $mo = 'HOURLY', [int][math]::Max(1, [math]::Round($IntervalMinutes / 60))
            }
            $null = schtasks /Create /TN $taskName `
                /TR "`"$runner`" `"$script`" --quiet" `
                /SC $sc /MO $mo /F 2>&1

            if ($LASTEXITCODE -eq 0) {
                $unit = if ($sc -eq 'MINUTE') { '分' } else { '時間' }
                Write-Host "[ok] タスク '$taskName' を schtasks で登録しました（$mo $unit おき）" -ForegroundColor Green
                Write-Host "     状態確認: schtasks /Query /TN $taskName /V /FO LIST"
                Write-Host "     解除:     .\run-dashboard.ps1 -UninstallTask"
                $null = schtasks /Run /TN $taskName 2>&1
                return
            }

            Write-Warning 'タスクの登録に失敗しました（Register-ScheduledTask / schtasks の両方）。'
            Write-Host '     多くの場合は権限不足です。次のいずれかを試してください:' -ForegroundColor Yellow
            Write-Host '       1) PowerShell を管理者として実行し、もう一度 -InstallTask を実行する'
            Write-Host '       2) タスクスケジューラ GUI で手動登録する'
            Write-Host "          プログラム: $python"
            Write-Host "          引数:       `"$script`" --quiet"
            Write-Host '       3) 自動収集を使わず、都度 .\run-dashboard.ps1 を実行する'
            exit 1
        }

        # 登録内容を読み戻して、繰り返し設定が本当に入ったか確認する
        $saved = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
        $rep = $saved.Triggers | Where-Object { $_.Repetition.Interval } |
            Select-Object -First 1 -ExpandProperty Repetition
        if (-not $rep) {
            Write-Warning '繰り返し設定が保存されませんでした。タスクスケジューラで確認してください。'
        }
        else {
            Write-Host "[ok] タスク '$taskName' を登録しました（間隔 $($rep.Interval) / ログオン時にも起動）" -ForegroundColor Green
        }
        Write-Host "     状態確認: Get-ScheduledTaskInfo -TaskName $taskName"
        Write-Host "     解除:     .\run-dashboard.ps1 -UninstallTask"
        try {
            Start-ScheduledTask -TaskName $taskName -ErrorAction Stop
            Write-Host '[ok] 初回実行を開始しました。'
        }
        catch {
            Write-Warning "初回実行の開始に失敗しました: $($_.Exception.Message)"
        }
        return
    }

    $UninstallTask {
        # 登録経路が Register-ScheduledTask / schtasks のどちらでも解除できるようにする。
        $null = schtasks /Query /TN $taskName 2>&1
        if ($LASTEXITCODE -ne 0) {
            Write-Host "[skip] タスク '$taskName' は登録されていません"
            return
        }
        try {
            Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction Stop
            Write-Host "[ok] タスク '$taskName' を解除しました" -ForegroundColor Green
        }
        catch {
            $null = schtasks /Delete /TN $taskName /F 2>&1
            if ($LASTEXITCODE -eq 0) {
                Write-Host "[ok] タスク '$taskName' を解除しました" -ForegroundColor Green
            }
            else {
                Write-Warning "タスクの解除に失敗しました: $($_.Exception.Message)"
                exit 1
            }
        }
        return
    }

    { [bool]$ExportCsv } {
        & $python (Join-Path $here 'aic_archive.py') --export-csv $ExportCsv
        if ($LASTEXITCODE -ne 0) { throw "CSV 出力に失敗しました (exit $LASTEXITCODE)" }
        return
    }

    { [bool]$BackupTo } {
        & $python (Join-Path $here 'aic_archive.py') --backup-to $BackupTo
        if ($LASTEXITCODE -ne 0) { throw "バックアップに失敗しました (exit $LASTEXITCODE)" }
        return
    }
}

# ---------------------------------------------------------------- 通常実行
if ($Stats) {
    & $python (Join-Path $here 'aic_archive.py') --stats
    if ($LASTEXITCODE -ne 0) { throw "統計の取得に失敗しました (exit $LASTEXITCODE)" }
    return
}

if ($Reconcile) {
    & $python (Join-Path $here 'aic_archive.py') --reconcile
    # exit 3 は「取りこぼしの兆候あり」で、実行自体は成功している。
    # 異常終了として扱うと、検出できたのに失敗に見えてしまう。
    if ($LASTEXITCODE -eq 3) {
        Write-Host ''
        Write-Warning '取りこぼしの兆候があります。上の内容を確認してください。'
        return
    }
    if ($LASTEXITCODE -ne 0) { throw "検算に失敗しました (exit $LASTEXITCODE)" }
    return
}

if ($TestAlert) {
    & $python (Join-Path $here 'aic_alert.py') --test
    if ($LASTEXITCODE -ne 0) {
        Write-Warning '通知を表示できませんでした。閾値超過は標準出力に出ます。'
    }
    return
}

if ($CheckAlert) {
    & $python (Join-Path $here 'aic_alert.py') --check
    if ($LASTEXITCODE -ne 0) { throw "閾値の評価に失敗しました (exit $LASTEXITCODE)" }
    return
}

if ($TuneAlert) {
    & $python (Join-Path $here 'aic_alert.py') --tune --target-per-month $TargetAlertsPerMonth
    if ($LASTEXITCODE -ne 0) { throw "通知頻度の実測に失敗しました (exit $LASTEXITCODE)" }
    return
}

& $python (Join-Path $here 'aic_collect.py')
if ($LASTEXITCODE -ne 0) { throw "集計に失敗しました (exit $LASTEXITCODE)" }

if ($Verify) {
    Write-Host ''
    & $python (Join-Path $here 'verify_pricing.py')
    if ($LASTEXITCODE -ne 0) { throw "換算検証に失敗しました (exit $LASTEXITCODE)" }
}

if (-not $NoOpen) {
    Start-Process (Join-Path $here 'index.html')
}
