<# Parser/function checks only. Never invoke the launcher or real process/file mutation. #>
$ErrorActionPreference = 'Stop'
$launcher = Join-Path $PSScriptRoot '..\start-v2-workers.ps1'
$errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile(
    (Resolve-Path -LiteralPath $launcher), [ref]$null, [ref]$errors)
if ($errors.Count) { throw ($errors.Message -join [Environment]::NewLine) }
function Assert-True($Value, [string]$Message) {
    if (-not $Value) { throw $Message }
}
$functions = @($ast.FindAll({
    param($node)
    $node -is [System.Management.Automation.Language.FunctionDefinitionAst]
}, $true))
foreach ($name in @('Same-Process','Confirm-BeatPidfile','Get-WorkerProcesses')) {
    $node = $functions | Where-Object Name -eq $name | Select-Object -First 1
    Assert-True ($null -ne $node) "Missing function $name"
    Invoke-Expression $node.Extent.Text
}
$script:alive = $false
$script:removed = $false
$script:pidText = '29192'
function Test-Path { param($LiteralPath) return $true }
function Get-Content { param($LiteralPath, [switch]$Raw) return $script:pidText }
function Get-Process { param($Id, $ErrorAction) if ($script:alive) { return @{Id=$Id} } }
function Remove-Item { param($LiteralPath, [switch]$Force) $script:removed = $true }
Confirm-BeatPidfile 'mock.pid'
Assert-True $script:removed 'Stale nonexistent PID was not removed'
$script:removed = $false
$script:alive = $true
$refused = $false
try { Confirm-BeatPidfile 'mock.pid' } catch { $refused = $true }
Assert-True ($refused -and -not $script:removed) 'Live PID file must be refused'
$script:alive = $false
$script:pidText = 'invalid'
$refused = $false
try { Confirm-BeatPidfile 'mock.pid' } catch { $refused = $true }
Assert-True ($refused -and -not $script:removed) 'Malformed PID file must remain for inspection'
$instant = [datetime]'2026-10-13T00:00:00Z'
$record = [PSCustomObject]@{pid=123;started=$instant.ToUniversalTime().Ticks.ToString();name='python';role='worker'}
$current = [PSCustomObject]@{Id=123;StartTime=$instant;ProcessName='python'}
Assert-True (Same-Process $current $record) 'Same PID/start time must match'
$current.StartTime = $instant.AddSeconds(1)
Assert-True (-not (Same-Process $current $record)) 'Reused PID must not match'
function Get-ServiceProcesses { param($Service) @($script:records) }
$script:records = @([PSCustomObject]@{pid=1;role='launcher'})
Assert-True (@(Get-WorkerProcesses ([PSCustomObject]@{child_observed=$true})).Count -eq 0) 'Dead child must not be hidden by live wrapper'
$script:records += [PSCustomObject]@{pid=2;role='worker'}
Assert-True (@(Get-WorkerProcesses ([PSCustomObject]@{child_observed=$true}))[0].pid -eq 2) 'Status must select actual child'
$script:records = @([PSCustomObject]@{pid=1;role='launcher'})
Assert-True (@(Get-WorkerProcesses ([PSCustomObject]@{child_observed=$false})).Count -eq 1) 'Standalone interpreter should remain supported'
$text = $ast.Extent.Text
Assert-True ($text.Contains('reliable-parallel-local@%h')) 'Second reliable hostname missing'
Assert-True ($text.Contains('All $($services.Count) services')) 'Service count must be dynamic'
Assert-True (-not $text.Contains('taskkill.exe')) 'Stop must not terminate unchecked process trees'
Write-Output '8 worker lifecycle checks passed; no processes or state were changed.'
