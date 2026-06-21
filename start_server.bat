@echo off
title LQ Hunter
cd /d "%~dp0"

echo ==============================================
echo   LQ Hunter  -  Telegram bot + chart server
echo ==============================================
echo.

rem --- refuse to start a second copy (port 8000 already in use) ---
python -c "import socket,sys;s=socket.socket();s.settimeout(0.4);r=s.connect_ex(('127.0.0.1',8000));s.close();sys.exit(0 if r==0 else 1)"
if not errorlevel 1 (
  echo LQ Hunter is already running on http://127.0.0.1:8000
  echo Opening it now. To restart, close the other LQ Hunter window first.
  start "" http://127.0.0.1:8000
  echo.
  pause
  exit /b
)

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
echo  error message ABOVE this line (ModuleNotFound = dependency,
echo  bind/10048 = another copy is already running).
echo ============================================================
pause
