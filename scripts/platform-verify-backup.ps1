[CmdletBinding()]
param([Parameter(Mandatory)] [string]$Package)
$ErrorActionPreference = "Stop"
$root = Resolve-Path (Join-Path $PSScriptRoot "..")
python (Join-Path $root "backend\scripts\platform_operations.py") verify --package $Package
exit $LASTEXITCODE
