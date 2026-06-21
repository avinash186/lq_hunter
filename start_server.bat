@echo off
title LQ Hunter
cd /d "%~dp0"

echo ==============================================
echo   LQ Hunter  -  Telegram bot + chart server
echo ==============================================
echo.
echo Checking dependencies (first run may take a minute)...
python -m pip install -q -r requirements.txt
echo.
echo Starting... Local chart: http://127.0.0.1:8000
echo Telegram: send /hub for a public link.  Close this window to stop.
echo.

rem open the local chart a few seconds after boot
start "" /min cmd /c "timeout /t 8 >nul && start http://127.0.0.1:8000"

python run.py

echo.
echo ============================================================
echo  LQ Hunter stopped. If this closed on its own, read the
echo  error message ABOVE this line (e.g. "address already in
echo  use" = it's already running; ModuleNotFound = dependency).
echo ============================================================
pause
