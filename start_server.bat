@echo off
title LQ Hunter
cd /d "%~dp0"

echo ==============================================
echo   LQ Hunter  -  Telegram bot + chart server
echo ==============================================
echo.

rem --- kill any previous LQ Hunter so we always start clean ---
echo Stopping any existing LQ Hunter instance...
powershell -NoProfile -Command "Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue | ForEach-Object { Stop-Process -Id $_.OwningProcess -Force -ErrorAction SilentlyContinue }; Get-CimInstance Win32_Process | Where-Object { $_.Name -eq 'python.exe' -and $_.CommandLine -match 'run\.py|server\.py|bot\.py' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }; Get-Process cloudflared -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue"
timeout /t 2 >nul

echo Checking dependencies (first run may take a minute)...
python -m pip install -q -r requirements.txt
echo.
echo Starting... a public cloudflared link will open in your browser and be
echo sent via Telegram automatically (set AUTO_HUB=0 in .env for localhost only).
echo Close this window to stop.
echo.

python run.py

echo.
echo ============================================================
echo  LQ Hunter stopped. Relaunch the shortcut anytime - it clears
echo  any old instance first, so you never need to exit carefully.
echo ============================================================
pause
