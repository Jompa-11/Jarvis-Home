<#
.SYNOPSIS
  Start JARVIS.
.EXAMPLE
  .\scripts\start.ps1              # opens the interface in your default browser
  .\scripts\start.ps1 -Desktop     # native window (requires: .\scripts\setup.ps1 -Desktop)
#>
param([switch]$Desktop, [switch]$NoBrowser)
$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root
$venvPy = Join-Path $Root '.venv\Scripts\python.exe'
if (-not (Test-Path $venvPy)) {
    Write-Host 'JARVIS is not set up yet. Run .\scripts\setup.ps1 first.' -ForegroundColor Yellow
    exit 1
}
$jarvisArgs = @('-m', 'jarvis')
if ($Desktop) { $jarvisArgs += '--desktop' }
if ($NoBrowser) { $jarvisArgs += '--no-browser' }
& $venvPy @jarvisArgs
