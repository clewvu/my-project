@echo off
cd /d "%~dp0"
.venv\Scripts\kalshi-bot.exe learn --live
echo.
echo To activate a proposed config: .venv\Scripts\kalshi-bot.exe learn --live --promote
pause
