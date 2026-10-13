[CmdletBinding()]
param(
  [Parameter(Mandatory)] [string]$Package,
  [Parameter(Mandatory)] [string]$Target,
  [Parameter(Mandatory)] [string]$DatabaseName,
  [Parameter(Mandatory)] [string]$Bucket,
  [switch]$Execute
)
$ErrorActionPreference = "Stop"
$root = Resolve-Path (Join-Path $PSScriptRoot "..")
$argsList = @((Join-Path $root "backend\scripts\platform_operations.py"), "restore-plan", "--package", $Package, "--target", $Target, "--database-name", $DatabaseName, "--bucket", $Bucket)
if ($Execute) { $argsList += "--execute" }
python @argsList
exit $LASTEXITCODE
