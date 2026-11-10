# Registers the scheduled task "KalshiLive": start deploy\run-live.ps1 at logon, in a
# minimized window, and start it again if the task itself fails. Run once; rerun to update.
# Remove with:  Unregister-ScheduledTask -TaskName KalshiLive -Confirm:$false

$root = Split-Path -Parent $PSScriptRoot
$script = Join-Path $PSScriptRoot 'run-live.ps1'
$pwsh = (Get-Command pwsh -ErrorAction Stop).Source
$user = "$env:USERDOMAIN\$env:USERNAME"

$action = New-ScheduledTaskAction -Execute $pwsh -WorkingDirectory $root `
    -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Minimized -File `"$script`""
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $user
$trigger.Delay = 'PT30S'  # let the network come up
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew `
    -RestartCount 10 -RestartInterval (New-TimeSpan -Minutes 2)
$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited

Register-ScheduledTask -TaskName 'KalshiLive' -Action $action -Trigger $trigger -Settings $settings `
    -Principal $principal -Description 'Kalshi live trader supervisor (kalshi-bot\deploy\run-live.ps1)' -Force |
    Select-Object TaskName, State
