# Local build only. This script never pushes or publishes.
[CmdletBinding()]
param([switch]$Offline)
$ErrorActionPreference = "Stop"
$arguments = @((Join-Path $PSScriptRoot "build_distribution.py"))
if ($Offline) { $arguments += "--offline" }
& python @arguments
if ($LASTEXITCODE -ne 0) { throw "MapTriggers local build failed ($LASTEXITCODE)." }
