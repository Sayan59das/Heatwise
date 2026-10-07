<#
  Live end-to-end check of the sensor + throttle pipeline. Self-elevates (one UAC prompt).

  1. probes the Intel MSRs and the Acer firmware fan interface (both read-only)
  2. runs the real backend elevated
  3. samples /api/snapshot at idle, then while a bounded all-core CPU load runs
  4. writes everything to backend\logs\verify_live.txt and stops everything it started

  The CPU burn is capped (default 25 s, hard cap 300 s) - it will make the fans spin up.
#>
param([int]$BurnSeconds = 25)

$principal = [Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Start-Process powershell -Verb RunAs -Wait -ArgumentList "-NoProfile -ExecutionPolicy Bypass -File `"$PSCommandPath`" -BurnSeconds $BurnSeconds"
    exit
}

$backend = Split-Path $PSScriptRoot -Parent
Set-Location $backend
$py  = Join-Path $backend ".venv\Scripts\python.exe"
$log = Join-Path $backend "logs\verify_live.txt"
New-Item -ItemType Directory -Force (Split-Path $log) | Out-Null
"ThermalSense live verification  $(Get-Date -Format s)  admin=$true" | Out-File -Encoding utf8 $log

function Log($text) { $text | Out-File -Encoding utf8 -Append $log }

$server = $null
try {
    $env:PYTHONIOENCODING = "utf-8"
    Log "`n=== MSR probe (Intel throttle flags / PL1 / PL2) ==="
    Log (& $py scripts\probe_msr.py --watch 3 2>&1 | ForEach-Object { "$_" })

    Log "`n=== Acer firmware probe (fans + EC temps vs LHM/NVML) ==="
    Log (& $py scripts\probe_acer_wmi.py --seconds 4 2>&1 | ForEach-Object { "$_" })

    $env:PYTHONIOENCODING = "utf-8"
    $server = Start-Process $py -ArgumentList "run.py" -WorkingDirectory $backend -PassThru -WindowStyle Hidden `
        -RedirectStandardError (Join-Path $backend "logs\server_verify.err") -RedirectStandardOutput (Join-Path $backend "logs\server_verify.out")
    Start-Sleep -Seconds 8

    Log "`n=== server log (warnings) ==="
    Log (Get-Content (Join-Path $backend "logs\server_verify.err") | Where-Object { $_ -match "WARNING|ERROR|healthy|ready" })

    Log "`n=== health ==="
    $h = Invoke-RestMethod http://127.0.0.1:8765/api/snapshot
    Log ($h.health | ConvertTo-Json -Depth 5)

    Log "`n=== idle (6 s) ==="
    Log (& $py scripts\sample_snapshots.py --seconds 6)

    Log "`n=== under load ($BurnSeconds s all-core burn) ==="
    $burn = Start-Process $py -ArgumentList "scripts\cpu_burn.py --seconds $BurnSeconds" -WorkingDirectory $backend -PassThru -WindowStyle Hidden
    Log (& $py scripts\sample_snapshots.py --seconds ($BurnSeconds + 6))
    if ($burn -and -not $burn.HasExited) { $burn.WaitForExit(10000) | Out-Null }

    Log "`n=== final snapshot (throttle + cpu limits) ==="
    $f = Invoke-RestMethod http://127.0.0.1:8765/api/snapshot
    Log ($f.throttle | ConvertTo-Json -Depth 6)
    Log ("pl1_w={0} pl2_w={1} pkg_w={2}" -f $f.cpu.pl1_w, $f.cpu.pl2_w, $f.cpu.package_power_w)
}
catch { Log "SCRIPT ERROR: $($_ | Out-String)" }
finally {
    Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
        Where-Object { $_.CommandLine -match 'run\.py|cpu_burn\.py' } |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
    Log "`n=== done; processes stopped ==="
}
