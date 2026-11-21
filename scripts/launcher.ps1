param(
    [ValidateSet("", "buddy", "player", "pair")]
    [string]$Mode = "",
    [ValidateSet("", "deepseek", "claude", "codex", "dsh")]
    [string]$Backend = "",
    [switch]$CheckOnly
)

$ErrorActionPreference = "Stop"
[Console]::InputEncoding = [System.Text.UTF8Encoding]::new($false)
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)

$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location $ProjectRoot

function Write-Title([string]$Text) {
    Write-Host ""
    Write-Host "============================================================" -ForegroundColor Cyan
    Write-Host "  $Text" -ForegroundColor Cyan
    Write-Host "============================================================" -ForegroundColor Cyan
}

function Find-Python311 {
    $candidates = @(
        @{ Exe = "py"; Prefix = @("-3.12") },
        @{ Exe = "py"; Prefix = @("-3.11") },
        @{ Exe = "py"; Prefix = @("-3") },
        @{ Exe = "python"; Prefix = @() },
        @{ Exe = "python3"; Prefix = @() }
    )
    foreach ($cand in $candidates) {
        $cmd = Get-Command $cand.Exe -ErrorAction SilentlyContinue
        if (-not $cmd) { continue }
        try {
            $args = @($cand.Prefix) + @("-c", "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
            $ver = (& $cand.Exe @args 2>$null | Select-Object -First 1)
            if ($ver -match "^(\d+)\.(\d+)$") {
                $maj = [int]$Matches[1]
                $min = [int]$Matches[2]
                if ($maj -gt 3 -or ($maj -eq 3 -and $min -ge 11)) {
                    return @{ Exe = $cand.Exe; Prefix = $cand.Prefix; Version = $ver }
                }
            }
        } catch {
            continue
        }
    }
    return $null
}

function Ensure-Venv {
    $venvDir = Join-Path $ProjectRoot ".venv-mcp"
    $venvPython = Join-Path $venvDir "Scripts\python.exe"
    if (-not (Test-Path $venvPython)) {
        $py = Find-Python311
        if (-not $py) {
            Write-Host "未检测到 Python 3.11 或更高版本。" -ForegroundColor Red
            $winget = Get-Command winget -ErrorAction SilentlyContinue
            if ($winget) {
                $ans = Read-Host "是否使用 winget 自动安装 Python 3.12？(Y/n)"
                if ($ans -eq "" -or $ans -match "^[Yy]") {
                    winget install -e --id Python.Python.3.12 --accept-package-agreements --accept-source-agreements
                    Write-Host "Python 安装完成后，请重新双击 setup_and_run.bat。" -ForegroundColor Yellow
                    exit 1
                }
            }
            Write-Host "请前往 https://www.python.org/downloads/ 安装 Python 3.11+（务必勾选 Add python.exe to PATH），然后重新运行。" -ForegroundColor Yellow
            exit 1
        }
        Write-Host "检测到 Python $($py.Version)，正在创建虚拟环境 .venv-mcp ..." -ForegroundColor Green
        $venvArgs = @($py.Prefix) + @("-m", "venv", $venvDir)
        & $py.Exe @venvArgs
        if ($LASTEXITCODE -ne 0 -or -not (Test-Path $venvPython)) {
            throw "创建虚拟环境 .venv-mcp 失败。"
        }
    }

    & $venvPython -c "import mcp" 2>$null
    if ($LASTEXITCODE -ne 0) {
        $req = Join-Path $ProjectRoot "python\requirements-mcp.txt"
        Write-Host "正在安装 MCP 依赖 (python/requirements-mcp.txt)..." -ForegroundColor Green
        & $venvPython -m pip install --upgrade pip -q
        & $venvPython -m pip install -r $req
        if ($LASTEXITCODE -ne 0) {
            Write-Host "默认 PyPI 源安装失败，尝试使用清华镜像源重试..." -ForegroundColor Yellow
            & $venvPython -m pip install -r $req -i https://pypi.tuna.tsinghua.edu.cn/simple
            if ($LASTEXITCODE -ne 0) {
                throw "安装依赖失败，请检查网络连接后重试。"
            }
        }
    }
    return $venvPython
}

function Get-EnvValue([string]$EnvPath, [string]$Key) {
    if (-not (Test-Path $EnvPath)) { return "" }
    foreach ($line in Get-Content $EnvPath -Encoding UTF8) {
        if ($line -match "^\s*#") { continue }
        if ($line -match "^\s*$([regex]::Escape($Key))\s*=\s*(.*)\s*$") {
            return $Matches[1].Trim().Trim('"').Trim("'")
        }
    }
    return ""
}

function Ensure-DeepSeekEnv {
    $envPath = Join-Path $ProjectRoot ".env"
    $examplePath = Join-Path $ProjectRoot ".env.example"
    $existing = $env:DEEPSEEK_API_KEY
    if (-not $existing) {
        $existing = Get-EnvValue $envPath "DEEPSEEK_API_KEY"
    }
    if ($existing) { return }

    Write-Host ""
    Write-Host "尚未检测到 DEEPSEEK_API_KEY。" -ForegroundColor Yellow
    Write-Host "密钥仅保存在本机 .env 文件中，不会上传到任何服务器或游戏内。" -ForegroundColor Gray
    $key = (Read-Host "请粘贴你的 DeepSeek API Key").Trim()
    if (-not $key) {
        throw "未输入 DEEPSEEK_API_KEY，已取消启动。"
    }
    if (-not (Test-Path $envPath)) {
        if (Test-Path $examplePath) {
            $content = Get-Content $examplePath -Raw -Encoding UTF8
            $content = [regex]::Replace($content, "(?m)^DEEPSEEK_API_KEY=.*$", "DEEPSEEK_API_KEY=$key")
        } else {
            $content = "DEEPSEEK_API_KEY=$key`nDEEPSEEK_API_URL=https://api.deepseek.com/chat/completions`nDEEPSEEK_MODEL=deepseek-flash`n"
        }
        [System.IO.File]::WriteAllText($envPath, $content, [System.Text.UTF8Encoding]::new($false))
    } else {
        $content = Get-Content $envPath -Raw -Encoding UTF8
        if ($content -match "(?m)^DEEPSEEK_API_KEY=") {
            $content = [regex]::Replace($content, "(?m)^DEEPSEEK_API_KEY=.*$", "DEEPSEEK_API_KEY=$key")
        } else {
            $content = $content.TrimEnd() + "`nDEEPSEEK_API_KEY=$key`n"
        }
        [System.IO.File]::WriteAllText($envPath, $content, [System.Text.UTF8Encoding]::new($false))
    }
    Write-Host "已保存到本机 .env。" -ForegroundColor Green
}

function Show-PairCommand([string]$VenvPython, [string]$Target) {
    if ($Target -eq "buddy") {
        $script = Join-Path $ProjectRoot "python\mcp_server\companion_server.py"
        $label = "DST Buddy Agent (AI 伙伴)"
    } else {
        $script = Join-Path $ProjectRoot "python\mcp_server\mcp_server.py"
        $label = "DST Player Agent (AI 角色操控)"
    }
    $pairCmd = (& $VenvPython $script --print-pair-command | Select-Object -First 1).Trim()
    if (-not $pairCmd) { throw "获取配对口令失败。" }
    try {
        Set-Clipboard -Value $pairCmd
        $copied = $true
    } catch {
        $copied = $false
    }
    Write-Host ""
    Write-Host "[$label] 游戏内配对口令（仅本机有效，请勿发给他人）：" -ForegroundColor Cyan
    Write-Host "  $pairCmd" -ForegroundColor Yellow
    if ($copied) {
        Write-Host "已自动复制到剪贴板！进游戏按 Y 打开聊天栏，直接 Ctrl+V 回车即可完成配对。" -ForegroundColor Green
    } else {
        Write-Host "请复制上方整行命令，进游戏按 Y 打开聊天栏粘贴回车即可配对。" -ForegroundColor Green
    }
    Write-Host "（纯本地拦截，绝不发到公屏；配对信息保存在 client_save，工坊更新不会覆盖）" -ForegroundColor Gray
    Write-Host ""
}

Write-Title "DST Hierarchical Agent 一键安装与启动器"
$venvPython = Ensure-Venv
Write-Host "运行环境已就绪：$venvPython" -ForegroundColor Green

if ($CheckOnly) {
    exit 0
}

if (-not $Mode) {
    Write-Host ""
    Write-Host "请选择要启动的模式："
    Write-Host "  [1] DST Buddy Agent  - AI 伙伴模式（推荐，支持最多 2 名 AI 威尔逊）"
    Write-Host "  [2] DST Player Agent - AI 角色操控模式（纯客户端，AI 操控你的角色）"
    Write-Host "  [3] 仅生成/复制游戏内配对口令（Buddy 与 Player）"
    $choice = (Read-Host "请输入编号 [默认 1]").Trim()
    switch ($choice) {
        ""  { $Mode = "buddy" }
        "1" { $Mode = "buddy" }
        "2" { $Mode = "player" }
        "3" { $Mode = "pair" }
        default { throw "无效选项：$choice" }
    }
}

if ($Mode -eq "pair") {
    Show-PairCommand $venvPython "player"
    Show-PairCommand $venvPython "buddy"
    Write-Host "提示：最后一次复制到剪贴板的是 Buddy 配对口令。" -ForegroundColor Gray
    Read-Host "按回车键退出"
    exit 0
}

if (-not $Backend) {
    Write-Host ""
    Write-Host "请选择规划后端："
    Write-Host "  [1] DeepSeek API（默认，使用本机 .env 中的 DEEPSEEK_API_KEY）"
    Write-Host "  [2] Claude Code CLI（--backend claude）"
    Write-Host "  [3] Codex CLI（--backend codex）"
    Write-Host "  [4] DSH CLI（--backend dsh，调用 dsh-tui）"
    $bChoice = (Read-Host "请输入编号 [默认 1]").Trim()
    switch ($bChoice) {
        ""  { $Backend = "deepseek" }
        "1" { $Backend = "deepseek" }
        "2" { $Backend = "claude" }
        "3" { $Backend = "codex" }
        "4" { $Backend = "dsh" }
        default { throw "无效选项：$bChoice" }
    }
}

if ($Backend -eq "deepseek") {
    Ensure-DeepSeekEnv
} else {
    $cliMap = @{ claude = "claude"; codex = "codex"; dsh = "dsh-tui" }
    $cliExe = $cliMap[$Backend]
    if (-not (Get-Command $cliExe -ErrorAction SilentlyContinue)) {
        Write-Host "警告：未在 PATH 中检测到 $cliExe，请确认已安装该 CLI，否则启动后规划会失败。" -ForegroundColor Yellow
    }
}

Show-PairCommand $venvPython $Mode

Write-Host "正在启动控制器（按 Ctrl+C 随时停止）..." -ForegroundColor Green
if ($Mode -eq "buddy") {
    & $venvPython (Join-Path $ProjectRoot "python\mcp_agent\companion_agent.py") --execute --backend $Backend
} else {
    & $venvPython (Join-Path $ProjectRoot "python\mcp_agent\mcp_agent.py") --loop --execute --backend $Backend
}
exit $LASTEXITCODE
