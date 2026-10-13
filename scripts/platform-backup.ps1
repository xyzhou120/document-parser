[CmdletBinding()]
param(
  [Parameter(Mandatory)] [string]$DatabaseDump,
  [Parameter(Mandatory)] [string]$Objects,
  [Parameter(Mandatory)] [string]$Target,
  [Parameter(Mandatory)] [string]$ConsistencyNote,
  [switch]$Execute
)
$ErrorActionPreference = "Stop"
$root = Resolve-Path (Join-Path $PSScriptRoot "..")
$argsList = @((Join-Path $root "backend\scripts\platform_operations.py"), "backup", "--database-dump", $DatabaseDump, "--objects", $Objects, "--target", $Target, "--consistency-note", $ConsistencyNote)
if ($Execute) { $argsList += "--execute" }
python @argsList
exit $LASTEXITCODE
