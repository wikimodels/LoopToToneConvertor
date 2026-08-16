@echo off
title LoopToToneConvertor Web
cd /d "%~dp0"

echo [~] Stopping old server (if running)...
for /f "tokens=5" %%a in ('netstat -aon ^| findstr ":8002 " ^| findstr "LISTENING"') do (
    taskkill /F /T /PID %%a >nul 2>&1
)

timeout /t 2 /nobreak >nul

echo [*] Starting server at http://localhost:8002
start "" "http://localhost:8002"
python web/server.py > server.log 2>&1