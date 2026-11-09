@echo off
cd /d "%~dp0"
echo LIVE mode: real orders on production. Ctrl-C to stop.
.venv\Scripts\kalshi-bot.exe go --live --acknowledge I-ACCEPT-LIVE-TRADING-RISK --count 1 --max-contracts 1 --max-open 2 --daily-loss-cap 10 --margin 0.04
pause
