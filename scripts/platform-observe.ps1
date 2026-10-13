[CmdletBinding()]
param([string]$BaseUrl = "http://127.0.0.1:8000", [switch]$Execute, [string]$Report)
$ErrorActionPreference = "Stop"
$argsList = @((Join-Path $PSScriptRoot "..\backend\scripts\platform_operations.py"), "observe", "--base-url", $BaseUrl)
if ($Execute) { $argsList += "--execute" }
if ($Report) { $argsList += @("--report", $Report) }
python @argsList
exit $LASTEXITCODE
