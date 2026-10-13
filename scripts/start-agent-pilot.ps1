param([int]$ApiPort = 8001, [int]$WebPort = 3001, [string]$Queue = 'checklist_agent_pilot')
$ErrorActionPreference = 'Stop'
$pilotRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$pilotLog = Join-Path $pilotRoot 'output/agent_evaluation/runtime'
New-Item -ItemType Directory -Path $pilotLog -Force | Out-Null
$pilotPython = Join-Path $pilotRoot 'backend/.venv/Scripts/python.exe'
$pilotNode = (Get-Command node -ErrorAction Stop).Source
$pilotVite = Join-Path $pilotRoot 'frontend/node_modules/vite/bin/vite.js'
if (!(Test-Path -LiteralPath $pilotPython) -or !(Test-Path -LiteralPath $pilotVite)) {
    throw 'Existing backend/.venv and frontend/node_modules are required; dependencies are not installed automatically.'
}
$pilotStarted = @()
$env:CELERY_TASK_DEFAULT_QUEUE = $Queue
$env:VITE_API_BASE_URL = "http://127.0.0.1:$ApiPort/api"
$workerStateFile = Join-Path $pilotLog "worker-$Queue.json"
function Test-PilotWorkerAlive($SavedWorker) {
    if (!$SavedWorker) { return $false }
    try {
        $process = Get-Process -Id ([int]$SavedWorker.process_id) -ErrorAction Stop
        return $process.StartTime.ToString('o') -eq $SavedWorker.started_at -and $process.Path -eq $pilotPython
    } catch { return $false }
}
function Start-PilotProcess($Name, $Executable, $Arguments, $Directory) {
    $process = Start-Process -FilePath $Executable -ArgumentList $Arguments -WorkingDirectory $Directory -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput (Join-Path $pilotLog "$Name.log") -RedirectStandardError (Join-Path $pilotLog "$Name-error.log")
    return [pscustomobject]@{ name = $Name; process_id = $process.Id; started_at = $process.StartTime.ToString('o') }
}
try {
    $health = Invoke-RestMethod "http://127.0.0.1:$ApiPort/health" -TimeoutSec 3
} catch { $health = $null }
if (!$health) {
    $pilotStarted += Start-PilotProcess 'api' $pilotPython @('-m', 'uvicorn', 'app.main:app', '--host', '127.0.0.1', '--port', "$ApiPort") (Join-Path $pilotRoot 'backend')
} else { Write-Output "API already online: $ApiPort" }
$savedWorker = if (Test-Path -LiteralPath $workerStateFile) { Get-Content -LiteralPath $workerStateFile -Raw | ConvertFrom-Json } else { $null }
if (Test-PilotWorkerAlive $savedWorker) {
    Write-Output "Worker already online: PID $($savedWorker.process_id), queue $Queue"
} else {
    $worker = Start-PilotProcess 'worker' $pilotPython @('-m', 'celery', '-A', 'app.celery_app', 'worker', '--pool=solo', '--loglevel=info', "--queues=$Queue", "--hostname=agent-pilot-$([guid]::NewGuid().ToString('N').Substring(0,8))@%h") (Join-Path $pilotRoot 'backend')
    $pilotStarted += $worker
    $worker | ConvertTo-Json | Set-Content -LiteralPath $workerStateFile -Encoding utf8
}
$pilotWeb = Get-NetTCPConnection -State Listen -LocalPort $WebPort -ErrorAction SilentlyContinue
if (!$pilotWeb) {
    $pilotStarted += Start-PilotProcess 'web' $pilotNode @('node_modules/vite/bin/vite.js', '--host', '127.0.0.1', '--port', "$WebPort", '--strictPort') (Join-Path $pilotRoot 'frontend')
} else { Write-Output "Web port already online: $WebPort" }
$state = [pscustomobject]@{ workspace = $pilotRoot; api = "http://127.0.0.1:$ApiPort"; web = "http://127.0.0.1:$WebPort"; queue = $Queue; processes = $pilotStarted }
$state | ConvertTo-Json -Depth 5 | Set-Content (Join-Path $pilotLog "started-$((Get-Date).ToString('yyyyMMdd-HHmmss')).json") -Encoding utf8
Start-Sleep -Seconds 5
Invoke-RestMethod "http://127.0.0.1:$ApiPort/health" -TimeoutSec 15 | Out-Null
Write-Output "Trial URL: http://127.0.0.1:$WebPort/upload"
Write-Output "API docs: http://127.0.0.1:$ApiPort/docs"
Write-Output "Queue: $Queue; logs: $pilotLog"
