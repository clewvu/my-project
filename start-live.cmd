@echo off
cd /d "%~dp0"
echo LIVE mode: real orders on production. Settings: deploy\live-args.txt
echo Restarts itself if it exits. Ctrl-C or stop-live.cmd to stop.
if exist state\KILL del state\KILL
pwsh -NoProfile -ExecutionPolicy Bypass -File "%~dp0deploy\run-live.ps1"
pause
