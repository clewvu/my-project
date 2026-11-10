# Keeps the live trader running: starts it, restarts it if it exits, one instance only.
#
# Started at logon by the scheduled task "KalshiLive" (see install-autostart.ps1) and
# safe to run by hand. Stop it with Ctrl-C in its window, or run stop-live.cmd.
# While state\KILL exists the trader places no orders and this script does not relaunch.
#
# Trading settings live in deploy\live-args.txt (one line of kalshi-bot arguments).

$ErrorActionPreference = 'Continue'
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
$exe = Join-Path $root '.venv\Scripts\kalshi-bot.exe'
$argsFile = Join-Path $PSScriptRoot 'live-args.txt'
$logDir = Join-Path $root 'state\logs'
$killFile = Join-Path $root 'state\KILL'
New-Item -ItemType Directory -Force $logDir | Out-Null
try { $host.UI.RawUI.WindowTitle = 'Kalshi LIVE (Ctrl-C to stop)' } catch {}

# One supervisor at a time: a second copy (task + manual start) exits straight away.
$created = $false
$mutex = New-Object System.Threading.Mutex($true, 'Local\KalshiLiveSupervisor', [ref]$created)
if (-not $created) {
    Write-Host 'Another Kalshi live supervisor is already running; exiting.'
    Start-Sleep -Seconds 3
    exit 0
}

function Write-Sup([string]$msg) {
    $line = '{0} SUPERVISOR {1}' -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $msg
    Write-Host $line
    Add-Content -Path (Join-Path $logDir ('live-{0}.log' -f (Get-Date -Format 'yyyyMMdd'))) -Value $line -Encoding utf8
}

try {
    $quick = 0
    while ($true) {
        if (Test-Path $killFile) {
            Write-Sup 'state\KILL exists; not starting. Delete it to resume.'
            while (Test-Path $killFile) { Start-Sleep -Seconds 15 }
            Write-Sup 'state\KILL removed; resuming.'
        }
        # A trader left over from an older launch would double every order.
        $stale = @(Get-CimInstance Win32_Process -Filter "Name='kalshi-bot.exe'" |
            Where-Object { $_.CommandLine -match '--live' -and $_.CommandLine -match '\s(go|trade)\s' })
        if ($stale.Count) {
            Write-Sup ('stopping {0} leftover live trader process(es)' -f $stale.Count)
            $stale | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
            Start-Sleep -Seconds 2
        }
        if (-not (Test-Path $argsFile)) { Write-Sup "missing $argsFile; cannot start"; break }
        $botArgs = (Get-Content $argsFile | Where-Object { $_ -and $_ -notmatch '^\s*#' }) -join ' ' -split '\s+' | Where-Object { $_ }
        $log = Join-Path $logDir ('live-{0}.log' -f (Get-Date -Format 'yyyyMMdd'))
        Write-Sup ('starting: kalshi-bot ' + (($botArgs -join ' ') -replace 'I-ACCEPT\S+', '<ack>'))
        $started = Get-Date
        & $exe @botArgs 2>&1 | ForEach-Object { "$_" } | Tee-Object -FilePath $log -Append -Encoding utf8
        $code = $LASTEXITCODE
        $ran = ((Get-Date) - $started).TotalSeconds
        # Back off if it keeps dying at once (bad key, no network at boot, bad arguments).
        if ($ran -lt 60) { $quick++ } else { $quick = 0 }
        $wait = [Math]::Min(300, 15 * [Math]::Pow(2, [Math]::Min($quick, 5)))
        Write-Sup ('trader exited (code {0}) after {1:n0}s; restarting in {2:n0}s' -f $code, $ran, $wait)
        Start-Sleep -Seconds $wait
    }
}
finally {
    $mutex.ReleaseMutex()
    $mutex.Dispose()
}
