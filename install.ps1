$ErrorActionPreference = "Stop"
[Console]::InputEncoding = [System.Text.UTF8Encoding]::new($false)
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)

$RepoUrl = "https://github.com/wjc1207/DST-Hierarchical-Agent.git"
$ZipUrl = "https://github.com/wjc1207/DST-Hierarchical-Agent/archive/refs/heads/main.zip"
$InstallDir = if ($env:DST_AGENT_HOME) { $env:DST_AGENT_HOME } else { Join-Path $env:USERPROFILE "DST-Hierarchical-Agent" }

Write-Host ""
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host "  DST Hierarchical Agent 一键安装程序" -ForegroundColor Cyan
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host "安装目录：$InstallDir" -ForegroundColor Gray

$git = Get-Command git -ErrorAction SilentlyContinue
if ($git) {
    if (Test-Path (Join-Path $InstallDir ".git")) {
        Write-Host "检测到已有仓库，正在更新到最新版本..." -ForegroundColor Green
        git -C $InstallDir pull --ff-only
        if ($LASTEXITCODE -ne 0) {
            Write-Host "git pull 未能快进，继续使用当前本地版本。" -ForegroundColor Yellow
        }
    } elseif (-not (Test-Path $InstallDir)) {
        Write-Host "正在使用 Git 克隆仓库..." -ForegroundColor Green
        git clone --depth 1 $RepoUrl $InstallDir
        if ($LASTEXITCODE -ne 0) { throw "Git 克隆失败，请检查网络连接。" }
    } else {
        Write-Host "目录已存在，直接启动安装器..." -ForegroundColor Yellow
    }
} else {
    Write-Host "未检测到 Git，正在下载源码压缩包..." -ForegroundColor Green
    $tmpRoot = Join-Path ([System.IO.Path]::GetTempPath()) ("dst_agent_install_" + [guid]::NewGuid().ToString("N"))
    $zipPath = Join-Path $tmpRoot "repo.zip"
    New-Item -ItemType Directory -Path $tmpRoot -Force | Out-Null
    try {
        Invoke-WebRequest -Uri $ZipUrl -OutFile $zipPath -UseBasicParsing
        Expand-Archive -Path $zipPath -DestinationPath $tmpRoot -Force
        $extracted = Get-ChildItem -Path $tmpRoot -Directory | Select-Object -First 1
        if (-not $extracted) { throw "解压压缩包失败。" }
        New-Item -ItemType Directory -Path $InstallDir -Force | Out-Null
        $preserve = @(".env", "logs", ".venv-mcp")
        foreach ($item in Get-ChildItem -Path $extracted.FullName -Force) {
            if ($preserve -contains $item.Name) { continue }
            if ($item.Name -eq "data") {
                $destData = Join-Path $InstallDir "data"
                New-Item -ItemType Directory -Path $destData -Force | Out-Null
                foreach ($dataItem in Get-ChildItem -Path $item.FullName -Force) {
                    if ($dataItem.Name -eq "agent_memory") { continue }
                    Copy-Item -Path $dataItem.FullName -Destination $destData -Recurse -Force
                }
                continue
            }
            $dest = Join-Path $InstallDir $item.Name
            if ($item.PSIsContainer) {
                Copy-Item -Path $item.FullName -Destination $InstallDir -Recurse -Force
            } else {
                Copy-Item -Path $item.FullName -Destination $dest -Force
            }
        }
    } finally {
        Remove-Item -Path $tmpRoot -Recurse -Force -ErrorAction SilentlyContinue
    }
}

$launcher = Join-Path $InstallDir "scripts\launcher.ps1"
if (-not (Test-Path $launcher)) {
    throw "未找到启动脚本：$launcher"
}
Write-Host "下载/更新完成！下次可直接双击 $InstallDir\setup_and_run.bat 启动。" -ForegroundColor Green
& $launcher
