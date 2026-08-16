@echo off
setlocal
title LoopToToneConvertor - install native host
cd /d "%~dp0"

set ID=%~1
if "%ID%"=="" (
    echo Usage:  install_host.cmd ^<extension-id^>
    echo.
    echo The extension id is shown in chrome://extensions and also
    echo displayed inside the panel if "Start server" is unavailable.
    echo.
    echo Example: install_host.cmd koehakhfofhdlhghiheiahnjgmempgdm
    exit /b 1
)

set REPO=%~dp0
set REPO=%REPO:~0,-1%
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0host\install_host.ps1" -ExtensionId "%ID%" -Repo "%REPO%"

echo.
echo [*] Done. Reload the extension (chrome://extensions - Reload) and
echo     click "Start server" in the side panel.
endlocal