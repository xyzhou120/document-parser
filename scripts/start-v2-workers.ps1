<# Local launcher for legacy/reliable/assistant Celery workers and beat. #>
[CmdletBinding()]
param(
    [ValidateSet('Start','Status','Stop')][string]$Action = 'Start',
    [switch]$DryRun
)
$ErrorActionPreference = 'Stop'
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$backendPath = Join-Path $projectRoot 'backend'
$pythonPath = Join-Path $backendPath '.venv\Scripts\python.exe'
$logDirectory = Join-Path $projectRoot 'output\workers'
$statePath = Join-Path $logDirectory 'v2-workers.json'
if (-not (Test-Path -LiteralPath $pythonPath)) { throw "Python environment not found: $pythonPath" }

function Get-Record($Process) {
    [PSCustomObject]@{ pid=[int]$Process.Id; started=$Process.StartTime.ToUniversalTime().Ticks.ToString(); name=[string]$Process.ProcessName }
}
function Same-Process($Process, $Record) {
    $Process -and [int]$Process.Id -eq [int]$Record.pid -and $Process.StartTime.ToUniversalTime().Ticks.ToString() -eq $Record.started -and [string]$Process.ProcessName -eq $Record.name
}
function Get-Owned($State) {
    if (-not $State) { return @() }
    $records = @($State.services | ForEach-Object { $_.processes })
    @($records | Where-Object { $record=$_; $process=Get-Process -Id $record.pid -ErrorAction SilentlyContinue; Same-Process $process $record })
}
function Stop-Owned($Records) {
    foreach ($record in $Records) {
        $current=Get-Process -Id $record.pid -ErrorAction SilentlyContinue
        if (Same-Process $current $record) {
            & taskkill.exe /PID $record.pid /T /F
            if ($LASTEXITCODE -ne 0 -and (Get-Process -Id $record.pid -ErrorAction SilentlyContinue)) { throw "Cannot stop PID $($record.pid)." }
        }
    }
}
function Save-State($Services) {
    [PSCustomObject]@{ project_root=$projectRoot; started_at=(Get-Date).ToString('o'); services=@($Services) } | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath $statePath -Encoding UTF8
}
function Quote-Argument([string]$Value) {
    '"' + (($Value -replace '(\\*)"', '$1$1\"') -replace '(\\+)$', '$1$1') + '"'
}

try {
$sha = [System.Security.Cryptography.SHA256]::Create()
try { $lockId = ([BitConverter]::ToString($sha.ComputeHash([Text.Encoding]::UTF8.GetBytes($projectRoot)))).Replace('-','') } finally { $sha.Dispose() }
$launchMutex = New-Object System.Threading.Mutex($false, "Local\document-parser-workers-$lockId")
$lockHeld = $false
try { $lockHeld = $launchMutex.WaitOne(0) } catch [System.Threading.AbandonedMutexException] { $lockHeld = $true }
if (-not $lockHeld) { throw 'Another worker launcher is active. Try again after it finishes.' }
$state = if (Test-Path -LiteralPath $statePath) { Get-Content -LiteralPath $statePath -Raw | ConvertFrom-Json } else { $null }
if ($state -and $state.project_root -ne $projectRoot) { throw 'Worker state belongs to another project directory.' }
$owned = @(Get-Owned $state)
if ($Action -eq 'Status') {
    if (-not $state) { Write-Host 'No v2 worker state file.'; exit 0 }
    foreach ($service in $state.services) {
        $running = @($service.processes | Where-Object { $r=$_; $owned | Where-Object { $_.pid -eq $r.pid } }).Count -gt 0
        [PSCustomObject]@{ Service=$service.name; State=if($running){'Running'}else{'Stopped'}; PID=(($service.processes.pid)-join ','); Log=$service.stderr }
    }
    exit 0
}
if ($Action -eq 'Stop') {
    if (-not $DryRun) { Stop-Owned $owned; Remove-Item -LiteralPath $statePath -Force -ErrorAction SilentlyContinue }
    else { $owned | Select-Object pid,name,started }
    exit 0
}
if ($owned.Count -gt 0) { throw 'Managed v2 workers already appear to be running. Use -Action Status or Stop first.' }

Push-Location $backendPath
try {
    $configCode = @'
import json,celery,redis
from app.config import settings
print(json.dumps({"queue":settings.celery.task_default_queue,"reliable":settings.app.reliable_enabled,"assistant":settings.app.assistant_enabled}))
'@
    $configJson = $configCode | & $pythonPath -
    if ($LASTEXITCODE -ne 0) { throw 'Cannot load Celery dependencies and backend configuration.' }
    $config = $configJson | ConvertFrom-Json
} finally { Pop-Location }
Write-Host "Legacy queue=$($config.queue), reliable_enabled=$($config.reliable), assistant_enabled=$($config.assistant)"
if (-not $config.reliable -or -not $config.assistant) { Write-Warning 'One or more v2 flags are disabled in the loaded configuration.' }
$definitions = @(
    @{ name='legacy'; args=@('worker','--pool=solo','--concurrency=1',"--queues=$($config.queue),reliable-control",'--hostname=legacy-local@%h') },
    @{ name='reliable'; args=@('worker','--pool=solo','--concurrency=1','--queues=reliable-execution','--hostname=reliable-local@%h') },
    @{ name='assistant'; args=@('worker','--pool=solo','--concurrency=1','--queues=assistant','--hostname=assistant-local@%h') },
    @{ name='beat'; args=@('beat',"--schedule=$(Join-Path $logDirectory 'celerybeat-schedule')", "--pidfile=$(Join-Path $logDirectory 'celerybeat.pid')") }
)
if ($DryRun) {
    foreach ($definition in $definitions) {
        Write-Host ("{0}: python -m celery -A app.celery_app:celery_app {1} --loglevel=INFO" -f $definition.name, ($definition.args -join ' '))
    }
    Write-Host 'Dry run complete. No workers were started or stopped.'
    exit 0
}
# Refuse existing manual workers, especially a second beat. Never stop them.
$manual = @(Get-CimInstance Win32_Process -ErrorAction Stop | Where-Object {
    $_.CommandLine -match 'app\.(celery_app(?::celery_app)?|tasks)["\s]+["\s]*(worker|beat)\b'
})
if ($manual.Count) { throw "Manually started Celery processes found (PIDs $($manual.ProcessId -join ',')). Stop those worker/beat terminals first." }
New-Item -ItemType Directory -Force -Path $logDirectory | Out-Null
$services=@(); $tag=Get-Date -Format 'yyyyMMdd-HHmmss-fff'
try {
    foreach ($definition in $definitions) {
        $stdout=Join-Path $logDirectory "$($definition.name)-$tag.stdout.log"; $stderr=Join-Path $logDirectory "$($definition.name)-$tag.stderr.log"
        $argumentLine=((@('-m','celery','-A','app.celery_app:celery_app')+$definition.args+@('--loglevel=INFO')) | ForEach-Object { Quote-Argument $_ }) -join ' '
        $process=Start-Process -FilePath $pythonPath -ArgumentList $argumentLine -WorkingDirectory $backendPath -WindowStyle Hidden -PassThru -RedirectStandardOutput $stdout -RedirectStandardError $stderr
        Start-Sleep -Milliseconds 300
        $entry=Get-Process -Id $process.Id -ErrorAction SilentlyContinue
        if (-not $entry) { throw "$($definition.name) exited; inspect $stderr" }
        $services += [PSCustomObject]@{name=$definition.name;stdout=$stdout;stderr=$stderr;processes=@((Get-Record $entry))}
        Save-State $services; Write-Host "Started $($definition.name) PID=$($process.Id)"
    }
    Start-Sleep -Seconds 3
    foreach ($service in $services) {
        if (-not @(Get-Owned ([PSCustomObject]@{services=@($service)})).Count) { throw "$($service.name) exited; inspect $($service.stderr)" }
    }
    Write-Host "All four processes are running. Logs are under $logDirectory"
    Write-Host 'Check stderr logs for broker connections and ready messages.'
} catch {
    Stop-Owned @(Get-Owned ([PSCustomObject]@{services=$services})); Remove-Item -LiteralPath $statePath -Force -ErrorAction SilentlyContinue; throw
}
} finally {
    if ($lockHeld) { $launchMutex.ReleaseMutex() }
    if ($launchMutex) { $launchMutex.Dispose() }
}
