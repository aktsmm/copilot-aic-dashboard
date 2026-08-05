<#
.SYNOPSIS
    Copilot ローカル AIC ダッシュボードの収集・表示・自動化。

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
#>
[CmdletBinding()]
param(
    [switch]$NoOpen,
    [switch]$Verify,
    [switch]$Stats,
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
        if ($cmd) { return $cmd.Source }
    }
    throw 'python が見つかりません。PATH を確認してください。'
}
$python = Get-PythonExe

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
        $action = New-ScheduledTaskAction -Execute $python `
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

        Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $triggers `
            -Settings $settings -Description 'Copilot AIC 使用量をアーカイブへ収集する' -Force | Out-Null

        # 登録内容を読み戻して、繰り返し設定が本当に入ったか確認する
        $saved = Get-ScheduledTask -TaskName $taskName
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
        Start-ScheduledTask -TaskName $taskName
        Write-Host '[ok] 初回実行を開始しました。'
        return
    }

    $UninstallTask {
        if (Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue) {
            Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
            Write-Host "[ok] タスク '$taskName' を解除しました" -ForegroundColor Green
        }
        else { Write-Host "[skip] タスク '$taskName' は登録されていません" }
        return
    }

    { [bool]$ExportCsv } {
        & $python (Join-Path $here 'aic_archive.py') --export-csv $ExportCsv
        return
    }

    { [bool]$BackupTo } {
        & $python (Join-Path $here 'aic_archive.py') --backup-to $BackupTo
        return
    }
}

# ---------------------------------------------------------------- 通常実行
if ($Stats) {
    & $python (Join-Path $here 'aic_archive.py') --stats
    return
}

& $python (Join-Path $here 'aic_collect.py')
if ($LASTEXITCODE -ne 0) { throw "集計に失敗しました (exit $LASTEXITCODE)" }

if ($Verify) {
    Write-Host ''
    & $python (Join-Path $here 'verify_pricing.py')
}

if (-not $NoOpen) {
    Start-Process (Join-Path $here 'index.html')
}
