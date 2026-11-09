@echo off
cd /d "%~dp0"
.venv\Scripts\kalshi-bot.exe trade-status --live
echo.
.venv\Scripts\kalshi-bot.exe trade-status
pause
