<# Stop only workers recorded by start-v2-workers.ps1. #>
[CmdletBinding()]
param([switch]$DryRun)
$ErrorActionPreference = 'Stop'
& (Join-Path $PSScriptRoot 'start-v2-workers.ps1') -Action Stop -DryRun:$DryRun
