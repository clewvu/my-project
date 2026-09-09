<#
  watchdog.ps1 - keep the six Kalshi two-desk processes alive.

  Relaunches any of the six that has died, so self-learning and trading continue
  across crashes and reboots. It is deliberately timid about the two real-money
  traders:
    * it will NOT relaunch a trader whose STOP file is present (you stopped it), and
    * it will NOT relaunch the crypto trader whose saved state says halted/stopped
      (e.g. a loss cap), and
    * if any process dies within 60s of being launched it backs off for 5 minutes
      instead of relaunching in a tight loop.
  Recorders, the learner and the dashboard are always kept up.

  Run it in its own window:
      powershell -ExecutionPolicy Bypass -File deploy\watchdog.ps1
  Or register it to start at logon (see deploy\install-watchdog.ps1).
  It adopts already-running processes, so starting it when all six are up launches
  nothing. Stop it with Ctrl-C; that does not stop the desks.
#>
param(
  [int]$IntervalSeconds = 30,
  [string]$Root = 'C:\Users\lewiscc2\kalshi-bot'
)
$ErrorActionPreference = 'Continue'
$venv   = Join-Path $Root '.venv\Scripts'
$bot    = Join-Path $venv 'kalshi-bot.exe'
$sports = Join-Path $venv 'kalshi-sports.exe'
$state  = Join-Path $Root 'state'
$log    = Join-Path $state 'watchdog.log'

function Write-Log($msg) {
  $line = "{0}  {1}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $msg
  try { Add-Content -Path $log -Value $line } catch {}
  Write-Host $line
}

# name; exe; args; CommandLine match; stop file (or $null); state file to check halt (or $null)
$specs = @(
  @{ name='crypto-record'; exe=$bot;    args=@('record');
     match='kalshi-bot\.exe" record'; stop=$null; state=$null },
  @{ name='sports-record'; exe=$sports; args=@('record');
     match='kalshi-sports\.exe" record'; stop=$null; state=$null },
  @{ name='crypto-learn';  exe=$bot;    args=@('learn','--every','3600');
     match='kalshi-bot\.exe" learn'; stop=$null; state=$null },
  @{ name='dashboard';     exe=$bot;    args=@('demo-ui','--port','8790');
     match='kalshi-bot\.exe" demo-ui'; stop=$null; state=$null },
  @{ name='crypto-live';   exe=$bot;    args=@('--env','prod','live-trade','--dollars','10','--max-dollars','20','--allow-external-positions','--real-money','--yes');
     match='kalshi-bot\.exe" --env prod live-trade(?!.*--series)'; stop=(Join-Path $state 'STOP'); state=(Join-Path $state 'live_loop.json') },
  @{ name='sports-live';   exe=$sports; args=@('--env','prod','live-trade','--real-money','--dollars','5','--max-dollars','10','--loss-cap','50','--yes');
     match='kalshi-sports\.exe" --env prod live-trade'; stop=(Join-Path $state 'SPORTS_STOP'); state=$null }
)

$lastLaunch   = @{}
$backoffUntil = @{}

function Test-Halted($stateFile) {
  if (-not $stateFile -or -not (Test-Path $stateFile)) { return $false }
  try { $s = Get-Content $stateFile -Raw | ConvertFrom-Json } catch { return $false }
  return [bool]$s.halted -or [bool]$s.stopped
}

Write-Log "watchdog start (interval ${IntervalSeconds}s), monitoring $($specs.Count) processes; root $Root"
while ($true) {
  $all = Get-CimInstance Win32_Process -ErrorAction SilentlyContinue
  foreach ($sp in $specs) {
    $now = Get-Date
    if ($backoffUntil[$sp.name] -and $now -lt $backoffUntil[$sp.name]) { continue }
    $running = $all | Where-Object { $_.CommandLine -match $sp.match }
    if ($running) { continue }

    if ($sp.stop -and (Test-Path $sp.stop)) { continue }              # you stopped it
    if (Test-Halted $sp.state) {                                       # halted itself (loss cap etc.)
      if (-not $backoffUntil[$sp.name]) { Write-Log "$($sp.name) halted in saved state; leaving it down" }
      $backoffUntil[$sp.name] = $now.AddMinutes(10)
      continue
    }
    if ($lastLaunch[$sp.name] -and ($now - $lastLaunch[$sp.name]).TotalSeconds -lt 60) {
      Write-Log "$($sp.name) died within 60s of launch; backing off 5 min (check its window/logs)"
      $backoffUntil[$sp.name] = $now.AddMinutes(5)
      continue
    }
    Write-Log "relaunching $($sp.name)"
    try { Start-Process -FilePath $sp.exe -ArgumentList $sp.args -WorkingDirectory $Root }
    catch { Write-Log "  failed to launch $($sp.name): $_" }
    $lastLaunch[$sp.name] = $now
    $backoffUntil.Remove($sp.name) | Out-Null
  }
  Start-Sleep -Seconds $IntervalSeconds
}
