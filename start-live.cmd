@echo off
cd /d "%~dp0"
echo LIVE mode: real orders on production. Ctrl-C to stop.
.venv\Scripts\kalshi-bot.exe go --live --acknowledge I-ACCEPT-LIVE-TRADING-RISK --exec hybrid --interval 2 --kelly 0.25 --count 2 --max-contracts 6 --max-open 4 --daily-loss-cap 15 --margin 0.025 --no-entry 60 --max-price 0.95 --bankroll 200
pause
