@echo off
cd /d "%~dp0"
echo PAPER mode: real prices, no real orders. Ctrl-C to stop.
.venv\Scripts\kalshi-bot.exe go --count 1 --max-contracts 1 --max-open 2 --daily-loss-cap 10 --margin 0.04
pause
