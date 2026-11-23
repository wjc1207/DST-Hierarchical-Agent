param(
    [Parameter(Mandatory = $true)]
    [string]$SourceDir,
    [string]$InstallDir = "",
    [ValidateSet("buddy", "player")]
    [string]$DefaultMode = "buddy",
    [switch]$Launch,
    [switch]$SkipShortcut,
    [switch]$CheckOnly
)

$ErrorActionPreference = "Stop"
[Console]::InputEncoding = [System.Text.UTF8Encoding]::new($false)
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)

if (-not $InstallDir) {
    $InstallDir = if ($env:DST_AGENT_HOME) { $env:DST_AGENT_HOME } else { Join-Path $env:USERPROFILE "DST-Hierarchical-Agent" }
}

$SourceRoot = (Resolve-Path $SourceDir).Path

function Write-Banner([string]$Title) {
    Write-Host ""
    Write-Host "============================================================" -ForegroundColor Cyan
    Write-Host "  $Title" -ForegroundColor Cyan
    Write-Host "============================================================" -ForegroundColor Cyan
}

function Sync-RunnerFiles([string]$FromDir, [string]$ToDir) {
    New-Item -ItemType Directory -Path $ToDir -Force | Out-Null
    $preserveRoot = @(".env", "logs", ".venv-mcp")
    foreach ($item in Get-ChildItem -Path $FromDir -Force) {
        if ($preserveRoot -contains $item.Name) { continue }
        if ($item.Name -eq "data") {
            $destData = Join-Path $ToDir "data"
            New-Item -ItemType Directory -Path $destData -Force | Out-Null
            foreach ($dataItem in Get-ChildItem -Path $item.FullName -Force) {
                if ($dataItem.Name -eq "agent_memory") { continue }
                Copy-Item -Path $dataItem.FullName -Destination $destData -Recurse -Force
            }
            continue
        }
        $dest = Join-Path $ToDir $item.Name
        if ($item.PSIsContainer) {
            Copy-Item -Path $item.FullName -Destination $ToDir -Recurse -Force
        } else {
            Copy-Item -Path $item.FullName -Destination $dest -Force
        }
    }

    # Write convenient batch entrypoint inside the persistent install directory.
    foreach ($pair in @(
        @{ File = "run_buddy.bat"; Mode = "buddy" }
    )) {
        $bat = @(
            "@echo off",
            "chcp 65001 >nul",
            "setlocal",
            'set "SCRIPT_DIR=%~dp0"',
            "where pwsh >nul 2>&1",
            "if %ERRORLEVEL%==0 (",
            "    pwsh -NoProfile -ExecutionPolicy Bypass -File `"%SCRIPT_DIR%scripts\launcher.ps1`" -Mode $($pair.Mode) %*",
            ") else (",
            "    powershell -NoProfile -ExecutionPolicy Bypass -File `"%SCRIPT_DIR%scripts\launcher.ps1`" -Mode $($pair.Mode) %*",
            ")",
            "if %ERRORLEVEL% NEQ 0 (",
            "    echo.",
            "    echo [Press any key to close...]",
            "    pause >nul",
            ")"
        ) -join "`r`n"
        [System.IO.File]::WriteAllText((Join-Path $ToDir $pair.File), $bat + "`r`n", [System.Text.UTF8Encoding]::new($false))
    }

    $srcVer = Join-Path $FromDir "version.txt"
    if (Test-Path $srcVer) {
        Copy-Item -Path $srcVer -Destination (Join-Path $ToDir ".workshop_runner_version") -Force
    }
}

function Needs-Sync([string]$FromDir, [string]$ToDir) {
    if (-not (Test-Path (Join-Path $ToDir "scripts\launcher.ps1"))) { return $true }
    $srcVer = Join-Path $FromDir "version.txt"
    $dstVer = Join-Path $ToDir ".workshop_runner_version"
    if (-not (Test-Path $srcVer)) { return $false }
    if (-not (Test-Path $dstVer)) { return $true }
    return ((Get-Content $srcVer -Raw).Trim() -ne (Get-Content $dstVer -Raw).Trim())
}

function Offer-DesktopShortcut([string]$ToDir, [string]$ModeName) {
    $label = if ($ModeName -eq "buddy") { "DST Buddy Agent" } else { "DST Player Agent" }
    $targetBat = if ($ModeName -eq "buddy") { Join-Path $ToDir "run_buddy.bat" } else { Join-Path $ToDir "run_player.bat" }
    $ans = (Read-Host "是否在桌面创建快捷方式「$label.lnk」（下次可直接从桌面一键启动，无需再找工坊目录）？[Y/n]").Trim()
    if ($ans -ne "" -and $ans -notmatch "^[Yy]") {
        Write-Host "已跳过创建桌面快捷方式。" -ForegroundColor Gray
        return
    }
    try {
        $desktop = [Environment]::GetFolderPath("Desktop")
        $lnkPath = Join-Path $desktop "$label.lnk"
        $shell = New-Object -ComObject WScript.Shell
        $shortcut = $shell.CreateShortcut($lnkPath)
        $shortcut.TargetPath = $targetBat
        $shortcut.WorkingDirectory = $ToDir
        $shortcut.Description = "Launch $label local Python MCP controller"
        $shortcut.Save()
        Write-Host "已创建桌面快捷方式：$lnkPath" -ForegroundColor Green
    } catch {
        Write-Host "创建桌面快捷方式失败（可手动运行 $targetBat）：$($_.Exception.Message)" -ForegroundColor Yellow
    }
}

if ($Launch) {
    if (Needs-Sync $SourceRoot $InstallDir) {
        Write-Host "正在同步工坊内置 Python 控制器到 $InstallDir ..." -ForegroundColor Green
        Sync-RunnerFiles $SourceRoot $InstallDir
    }
    $launcher = Join-Path $InstallDir "scripts\launcher.ps1"
    & $launcher -Mode $DefaultMode
    exit $LASTEXITCODE
}

Write-Banner "DST Hierarchical Agent 创意工坊一键安装程序"
Write-Host "离线源目录：$SourceRoot" -ForegroundColor Gray
Write-Host "本机安装目录：$InstallDir（独立于 Steam 工坊，工坊更新不会覆盖配置与记忆）" -ForegroundColor Gray

Write-Host ""
Write-Host "[1/3] 正在复制 Python 控制器到本机目录（保留已有 .env、令牌与记忆）..." -ForegroundColor Green
Sync-RunnerFiles $SourceRoot $InstallDir

$launcher = Join-Path $InstallDir "scripts\launcher.ps1"
if ($CheckOnly) {
    Write-Host "安装文件同步检查完成：$InstallDir" -ForegroundColor Green
    exit 0
}

Write-Host "[2/3] 正在检查 Python 3.11+ 与虚拟环境依赖..." -ForegroundColor Green
& $launcher -CheckOnly
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

Write-Host "[3/3] 安装完成！" -ForegroundColor Green
if (-not $SkipShortcut) {
    Offer-DesktopShortcut $InstallDir $DefaultMode
}

Write-Host ""
$runHint = if ($DefaultMode -eq "buddy") { "2_run_buddy.bat" } else { "2_run_player.bat" }
Write-Host "后续你可以随时双击本目录的 $runHint，或直接点击桌面快捷方式启动。" -ForegroundColor Cyan
$startNow = (Read-Host "是否现在立即启动控制器？[Y/n]").Trim()
if ($startNow -eq "" -or $startNow -match "^[Yy]") {
    & $launcher -Mode $DefaultMode
    exit $LASTEXITCODE
}
exit 0
