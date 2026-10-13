[CmdletBinding()]
param([string]$Root = (Resolve-Path (Join-Path $PSScriptRoot "..")))
$ErrorActionPreference = "Stop"
python (Join-Path $Root "backend/scripts/platform_operations.py") config-check --root $Root
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
