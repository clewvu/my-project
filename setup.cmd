@echo off
cd /d "%~dp0"
if not exist .venv\Scripts\kalshi-bot.exe (
  echo Installing...
  python -m venv .venv && .venv\Scripts\pip install -q -e ".[dev]"
)
.venv\Scripts\kalshi-bot.exe setup
pause
