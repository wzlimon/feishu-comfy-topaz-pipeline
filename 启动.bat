@echo off
chcp 65001 >nul 2>&1
set PYTHONUTF8=1
cd /d "%~dp0"
title Feishu Video Pipeline

echo ============================================================
echo   Feishu -^> ComfyUI -^> Topaz -^> Baidu Netdisk  Pipeline
echo ============================================================
echo.
echo   Keep this window open. It watches the Feishu table.
echo   Closing this window stops the service.
echo.

if not exist ".venv\Scripts\python.exe" (
    echo [ERROR] Virtual environment not found. Run the "install dependencies" batch file first.
    pause
    exit /b 1
)

.venv\Scripts\python.exe main.py
set RC=%errorlevel%

echo.
if %RC%==0 (
    echo Service stopped.
) else (
    echo [!] Startup failed (exit code %RC%). See error messages above.
    echo     Common causes: ComfyUI not started / Feishu credentials missing / workflow not exported.
    echo     If it says "waiting for ComfyUI", just open ComfyUI and it continues automatically.
)

pause
