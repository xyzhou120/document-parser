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

function Get-Record($Process, [string]$Role = 'worker') {
    [PSCustomObject]@{ pid=[int]$Process.Id; started=$Process.StartTime.ToUniversalTime().Ticks.ToString(); name=[string]$Process.ProcessName; role=$Role }
}
function Same-Process($Process, $Record) {
    $Process -and [int]$Process.Id -eq [int]$Record.pid -and $Process.StartTime.ToUniversalTime().Ticks.ToString() -eq $Record.started -and [string]$Process.ProcessName -eq $Record.name
}
function Get-Owned($State) {
    if (-not $State) { return @() }
    $records = @($State.services | ForEach-Object { $_.processes })
    @($records | Where-Object { $record=$_; $process=Get-Process -Id $record.pid -ErrorAction SilentlyContinue; Same-Process $process $record })
}
function Get-ServiceProcesses($Service) {
    $known = @(Get-Owned ([PSCustomObject]@{ services=@($Service) }))
    if (-not $known.Count) { return @() }
    $inventory = @(Get-CimInstance Win32_Process -ErrorAction Stop)
    $result = @($known)
    $parents = @($known.pid)
    # Venv python.exe is a launcher on Windows; record its real interpreter
    # descendants while their ancestry is still tied to the owned identity.
    for ($depth=0; $depth -lt 4 -and $parents.Count; $depth++) {
        $next = @()
        foreach ($child in $inventory | Where-Object { $parents -contains [int]$_.ParentProcessId }) {
            $parentRecord = $result | Where-Object { $_.pid -eq [int]$child.ParentProcessId } | Select-Object -First 1
            $parentProcess = Get-Process -Id $child.ParentProcessId -ErrorAction SilentlyContinue
            if (-not (Same-Process $parentProcess $parentRecord)) { continue }
            if ($child.Name -notmatch '^python(w)?\.exe$' -or
                $child.CommandLine -notmatch 'app\.celery_app(?::celery_app)?') { continue }
            $live = Get-Process -Id $child.ProcessId -ErrorAction SilentlyContinue
            if (-not $live) { continue }
            $record = Get-Record $live 'worker'
            if ([long]$record.started -lt [long]$parentRecord.started) { continue }
            $Service | Add-Member -NotePropertyName child_observed -NotePropertyValue $true -Force
            if (-not ($result | Where-Object { $_.pid -eq $record.pid -and $_.started -eq $record.started })) {
                $result += $record
            }
            $next += [int]$child.ProcessId
        }
        $parents = @($next)
    }
    @($result)
}
function Get-WorkerProcesses($Service) {
    $records = @(Get-ServiceProcesses $Service)
    $children = @($records | Where-Object { $_.role -eq 'worker' })
    if ($children.Count) { return $children }
    if ($Service.child_observed) { return @() }
    # A standalone interpreter need not spawn a venv child.
    @($records)
}
function Confirm-BeatPidfile([string]$Pidfile) {
    if (-not (Test-Path -LiteralPath $Pidfile)) { return }
    $raw = (Get-Content -LiteralPath $Pidfile -Raw).Trim()
    $beatProcessId = 0
    if (-not [int]::TryParse($raw, [ref]$beatProcessId) -or $beatProcessId -le 0) {
        throw "Invalid beat PID file; inspect it before removing: $Pidfile"
    }
    if (Get-Process -Id $beatProcessId -ErrorAction SilentlyContinue) {
        throw "Beat PID file names a live process (PID $beatProcessId); refusing to start a second beat."
    }
    Remove-Item -LiteralPath $Pidfile -Force
}
function Stop-Owned($Records) {
    foreach ($record in $Records | Sort-Object { [long]$_.started } -Descending) {
        $current=Get-Process -Id $record.pid -ErrorAction SilentlyContinue
        if (Same-Process $current $record) {
            # Stop only recorded process identities, never an unchecked /T tree.
            Stop-Process -Id $record.pid -Force -ErrorAction Stop
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
$owned = @()
if ($state) {
    foreach ($service in $state.services) { $owned += @(Get-ServiceProcesses $service) }
}
if ($Action -eq 'Status') {
    if (-not $state) { Write-Host 'No v2 worker state file.'; exit 0 }
    foreach ($service in $state.services) {
        $workers = @(Get-WorkerProcesses $service)
        $running = $workers.Count -gt 0
        [PSCustomObject]@{ Service=$service.name; State=if($running){'Running'}else{'Stopped'}; PID=(($workers.pid)-join ','); Log=$service.stderr }
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
    @{ name='reliable-parallel'; args=@('worker','--pool=solo','--concurrency=1','--queues=reliable-execution','--hostname=reliable-parallel-local@%h') },
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
Confirm-BeatPidfile (Join-Path $logDirectory 'celerybeat.pid')
$services=@(); $tag=Get-Date -Format 'yyyyMMdd-HHmmss-fff'
try {
    foreach ($definition in $definitions) {
        $stdout=Join-Path $logDirectory "$($definition.name)-$tag.stdout.log"; $stderr=Join-Path $logDirectory "$($definition.name)-$tag.stderr.log"
        $argumentLine=((@('-m','celery','-A','app.celery_app:celery_app')+$definition.args+@('--loglevel=INFO')) | ForEach-Object { Quote-Argument $_ }) -join ' '
        $process=Start-Process -FilePath $pythonPath -ArgumentList $argumentLine -WorkingDirectory $backendPath -WindowStyle Hidden -PassThru -RedirectStandardOutput $stdout -RedirectStandardError $stderr
        $entry=Get-Process -Id $process.Id -ErrorAction SilentlyContinue
        if (-not $entry) { throw "$($definition.name) exited; inspect $stderr" }
        $service = [PSCustomObject]@{name=$definition.name;stdout=$stdout;stderr=$stderr;child_observed=$false;processes=@((Get-Record $entry 'launcher'))}
        $services += $service
        Save-State $services
        $deadline = (Get-Date).AddSeconds(30)
        $ready = $false
        do {
            Start-Sleep -Milliseconds 250
            $service.processes = @(Get-ServiceProcesses $service)
            Save-State $services
            if (-not $service.processes.Count) { throw "$($definition.name) exited; inspect $stderr" }
            $workers = @(Get-WorkerProcesses $service)
            if ($definition.name -eq 'beat') {
                $pidfile = Join-Path $logDirectory 'celerybeat.pid'
                if (Test-Path -LiteralPath $pidfile) {
                    $beatId = (Get-Content -LiteralPath $pidfile -Raw).Trim()
                    $ready = @($workers | Where-Object { $_.pid.ToString() -eq $beatId }).Count -gt 0
                }
            } elseif ($workers.Count -and (Test-Path -LiteralPath $stderr)) {
                $ready = [bool](Select-String -LiteralPath $stderr -Pattern '\sready\.' -Quiet)
            }
        } while (-not $ready -and (Get-Date) -lt $deadline)
        if (-not $ready) { throw "$($definition.name) did not become ready; inspect $stderr" }
        Write-Host "Started $($definition.name) worker PID=$($workers.pid -join ',')"
    }
    Start-Sleep -Seconds 3
    foreach ($service in $services) {
        if (-not @(Get-WorkerProcesses $service).Count) { throw "$($service.name) exited; inspect $($service.stderr)" }
    }
    Write-Host "All $($services.Count) services are running. Logs are under $logDirectory"
    Write-Host 'Check stderr logs for broker connections and ready messages.'
} catch {
    $cleanup = @()
    foreach ($service in $services) { $cleanup += @(Get-ServiceProcesses $service) }
    Stop-Owned $cleanup; Remove-Item -LiteralPath $statePath -Force -ErrorAction SilentlyContinue; throw
}
} finally {
    if ($lockHeld) { $launchMutex.ReleaseMutex() }
    if ($launchMutex) { $launchMutex.Dispose() }
}
