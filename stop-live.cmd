@echo off
cd /d "%~dp0"
if not exist state mkdir state
echo stop > state\KILL
schtasks /End /TN KalshiLive >nul 2>&1
powershell -NoProfile -Command "Get-CimInstance Win32_Process | Where-Object { ($_.Name -eq 'kalshi-bot.exe' -and $_.CommandLine -match '--live') -or ($_.Name -eq 'pwsh.exe' -and $_.CommandLine -match 'run-live\.ps1') } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }"
echo Live trader stopped and state\KILL set, so it will not start again at logon.
echo Open positions settle on their own. Run start-live.cmd to resume.
pause
