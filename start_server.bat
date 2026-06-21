@echo off
title LQ Hunter Server
cd /d "%~dp0"
echo Starting LQ Hunter server...
echo The dashboard will open at http://127.0.0.1:8000
echo (Close this window to stop the server.)
echo.
rem open the browser a few seconds after the server starts booting
start "" /min cmd /c "timeout /t 6 >nul && start http://127.0.0.1:8000"
python server.py
echo.
echo Server stopped.
pause
