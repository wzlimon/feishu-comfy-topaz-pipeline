@echo off
chcp 65001 >nul 2>&1
set PYTHONUTF8=1
cd /d "%~dp0"
title Pipeline Health Check

if not exist ".venv\Scripts\python.exe" (
    echo [ERROR] Virtual environment not found. Run the "install dependencies" batch file first.
    pause
    exit /b 1
)

.venv\Scripts\python.exe doctor.py

echo.
pause
