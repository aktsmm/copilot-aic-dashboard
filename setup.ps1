<#
.SYNOPSIS
    copilot-aic-dashboard の初期セットアップ。

.DESCRIPTION
    1. 前提条件（Python / Copilot のローカル DB）を確認する
    2. アーカイブ DB の置き場所を決めて config.json に書く
    3. 初回の収集を実行する
    4. 1 時間ごとの自動収集をタスクスケジューラに登録する（任意）

    追加パッケージのインストールは不要。Python 標準ライブラリだけで動く。

.PARAMETER ArchiveDb
    アーカイブ DB のフルパス。省略するとホーム直下の
    .copilot-aic\archive.db を使う。
    ローカル DB を消しても消えない場所を選ぶこと。

.PARAMETER SkipTask
    タスクスケジューラへの登録を行わない。

.EXAMPLE
    .\setup.ps1
    対話なしで既定値のままセットアップする。

.EXAMPLE
    .\setup.ps1 -ArchiveDb 'D:\data\copilot\archive.db' -IntervalMinutes 30
#>
[CmdletBinding()]
param(
    [string]$ArchiveDb,
    [ValidateRange(1, 1440)][int]$IntervalMinutes = 60,
    [switch]$SkipTask,
    [switch]$NoOpen
)

$ErrorActionPreference = 'Stop'
$here = $PSScriptRoot
$env:PYTHONIOENCODING = 'utf-8'

function Write-Step { param($n, $m) Write-Host "`n[$n] $m" -ForegroundColor Cyan }
function Write-Ok { param($m) Write-Host "    OK  $m" -ForegroundColor Green }
function Write-Warn2 { param($m) Write-Host "    !!  $m" -ForegroundColor Yellow }

Write-Host 'copilot-aic-dashboard setup' -ForegroundColor White

# ---------------------------------------------------------------- 1. 前提条件
Write-Step 1 '前提条件を確認します'

$python = $null
foreach ($c in 'python', 'python3', 'py') {
    $cmd = Get-Command $c -ErrorAction SilentlyContinue
    if ($cmd) { $python = $cmd.Source; break }
}
if (-not $python) {
    throw 'python が見つかりません。https://www.python.org/downloads/ からインストールし、PATH を通してください。'
}
$pyVer = (& $python -c 'import sys; print("%d.%d" % sys.version_info[:2])').Trim()
if ([version]$pyVer -lt [version]'3.9') {
    throw "Python 3.9 以上が必要です（検出: $pyVer）"
}
Write-Ok "Python $pyVer ($python)"

$liveDb = Join-Path $env:USERPROFILE '.copilot\session-store.db'
if (Test-Path -LiteralPath $liveDb) {
    $size = [math]::Round((Get-Item -LiteralPath $liveDb).Length / 1MB, 1)
    Write-Ok "Copilot ローカル DB: $liveDb ($size MB)"
}
else {
    Write-Warn2 "Copilot ローカル DB が見つかりません: $liveDb"
    Write-Warn2 'GitHub Copilot CLI / App を一度使ってから再実行してください。'
    Write-Warn2 'デモだけ見る場合は .\run-dashboard.ps1 -Demo を実行できます。'
}

# ---------------------------------------------------------------- 2. 保存先
Write-Step 2 'アーカイブ DB の保存先を設定します'

if (-not $ArchiveDb) {
    $ArchiveDb = Join-Path $env:USERPROFILE '.copilot-aic\archive.db'
}
$archiveDir = Split-Path -Path $ArchiveDb -Parent
New-Item -ItemType Directory -Force -Path $archiveDir | Out-Null

if ($archiveDir -like "$env:USERPROFILE\.copilot*" -and $archiveDir -notlike "$env:USERPROFILE\.copilot-aic*") {
    Write-Warn2 'アーカイブを ~/.copilot 配下に置くと、ローカル DB の掃除で一緒に消える恐れがあります。'
}

$cfgPath = Join-Path $here 'config.json'
$cfg = Get-Content -LiteralPath $cfgPath -Raw -Encoding UTF8 | ConvertFrom-Json
$cfg | Add-Member -NotePropertyName archive_db -NotePropertyValue $ArchiveDb -Force
$cfg | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $cfgPath -Encoding UTF8
Write-Ok "アーカイブ: $ArchiveDb"
Write-Ok "設定を書き込みました: $cfgPath"

# ---------------------------------------------------------------- 3. 初回収集
Write-Step 3 '初回の収集を実行します'
& $python (Join-Path $here 'aic_collect.py')
if ($LASTEXITCODE -ne 0) { throw "収集に失敗しました (exit $LASTEXITCODE)" }

# ---------------------------------------------------------------- 4. 自動収集
if ($SkipTask) {
    Write-Step 4 '自動収集の登録はスキップしました'
    Write-Host '    後で登録する: .\run-dashboard.ps1 -InstallTask'
}
else {
    Write-Step 4 "自動収集を登録します（$IntervalMinutes 分ごと）"
    Write-Host '    ローカル DB が消される前に取り込むための保険です。'
    & (Join-Path $here 'run-dashboard.ps1') -InstallTask -IntervalMinutes $IntervalMinutes
}

Write-Host "`nセットアップ完了。" -ForegroundColor Green
Write-Host '  表示    : .\run-dashboard.ps1'
Write-Host '  統計    : .\run-dashboard.ps1 -Stats'
Write-Host '  バックアップ: .\run-dashboard.ps1 -BackupTo <dir>'

if (-not $NoOpen) { Start-Process (Join-Path $here 'index.html') }
