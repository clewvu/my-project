<#
  Installs the watchdog to auto-start at logon via the current user's Startup
  folder. No admin required (a Scheduled Task would need elevation).
  Run once:  powershell -ExecutionPolicy Bypass -File deploy\install-watchdog.ps1
  Disable:   delete the .vbs the script reports, or create the STOP files.
#>
$Root = 'C:\Users\lewiscc2\kalshi-bot'
$ps   = Join-Path $Root 'deploy\watchdog.ps1'
$startup = [Environment]::GetFolderPath('Startup')
$vbs  = Join-Path $startup 'KalshiWatchdog.vbs'

# launch the watchdog hidden (0 = no window) and don't wait
$run  = "powershell -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File ""$ps"""
$line = 'CreateObject("WScript.Shell").Run "' + ($run -replace '"', '""') + '", 0, False'
Set-Content -Path $vbs -Value $line -Encoding ASCII
Write-Host "Installed logon autostart: $vbs"
Write-Host "The watchdog will start hidden at each logon and keep the six desks alive."
