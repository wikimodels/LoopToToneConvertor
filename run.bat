@echo off
title LoopToToneConvertor Web
cd /d "%~dp0"

echo [~] Stopping old servers (by port 8002 and leftover web\server.py processes)...
for /f "tokens=5" %%a in ('netstat -aon ^| findstr ":8002 " ^| findstr "LISTENING"') do (
    taskkill /F /T /PID %%a >nul 2>&1
)
powershell -NoProfile -ExecutionPolicy Bypass -Command "Get-CimInstance Win32_Process | Where-Object { $_.Name -match '^python' -and $_.CommandLine -match 'web[\\/]server\.py' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }" >nul 2>&1

timeout /t 2 /nobreak >nul

echo [*] Starting server at http://localhost:8002
start "" "http://localhost:8002"
python web/server.py > server.log 2>&1