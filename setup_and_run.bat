@echo off
chcp 65001 >nul
setlocal
set "SCRIPT_DIR=%~dp0"
where pwsh >nul 2>&1
if %ERRORLEVEL%==0 (
    pwsh -NoProfile -ExecutionPolicy Bypass -File "%SCRIPT_DIR%scripts\launcher.ps1" %*
) else (
    powershell -NoProfile -ExecutionPolicy Bypass -File "%SCRIPT_DIR%scripts\launcher.ps1" %*
)
if %ERRORLEVEL% NEQ 0 (
    echo.
    echo [程序已退出，按任意键关闭窗口...]
    pause >nul
)
