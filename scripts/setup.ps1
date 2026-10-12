<#
.SYNOPSIS
  One-time setup of JARVIS on Windows 11.
.DESCRIPTION
  Creates a Python virtual environment in .venv, installs dependencies,
  and creates local config files from the examples if they do not exist.
  Changes nothing outside the project folder.
.PARAMETER Desktop
  Also install pywebview for the native desktop window.
.PARAMETER Dev
  Also install test dependencies (pytest).
#>
param([switch]$Desktop, [switch]$Dev)
$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

function Find-Python {
    foreach ($cand in @(@('py', '-3.13'), @('py', '-3.12'), @('py', '-3.11'), @('python'))) {
        try {
            $exe = $cand[0]; $args_ = @($cand | Select-Object -Skip 1)
            $ver = & $exe @args_ -c "import sys; print('%d.%d' % sys.version_info[:2])" 2>$null
            if ($LASTEXITCODE -eq 0 -and [version]$ver -ge [version]'3.11') { return ,$cand }
        } catch { }
    }
    return $null
}

$py = Find-Python
if (-not $py) {
    Write-Host 'Python 3.11 or newer was not found.' -ForegroundColor Red
    Write-Host 'Install it with:  winget install Python.Python.3.13   (then open a new terminal)'
    exit 1
}
Write-Host "Using Python: $($py -join ' ')" -ForegroundColor Cyan

if (-not (Test-Path '.venv\Scripts\python.exe')) {
    Write-Host 'Creating virtual environment (.venv)...'
    & $py[0] @($py | Select-Object -Skip 1) -m venv .venv
}
$venvPy = Join-Path $Root '.venv\Scripts\python.exe'

& $venvPy -m pip install --upgrade pip --quiet
$req = if ($Dev) { 'requirements-dev.txt' } else { 'requirements.txt' }
Write-Host "Installing $req ..."
& $venvPy -m pip install -r $req --quiet
if ($Desktop) {
    Write-Host 'Installing pywebview for the desktop window...'
    & $venvPy -m pip install 'pywebview>=5.0' --quiet
}

if (-not (Test-Path 'config\jarvis.toml')) {
    Copy-Item 'config\jarvis.example.toml' 'config\jarvis.toml'
    Write-Host 'Created config\jarvis.toml (edit it to configure Home Assistant, apps, profiles).'
}
if (-not (Test-Path '.env')) {
    Copy-Item '.env.example' '.env'
    Write-Host 'Created .env (add ANTHROPIC_API_KEY and HA_TOKEN there; it is git-ignored).'
}

Write-Host ''
& $venvPy -m jarvis check
Write-Host ''
Write-Host 'Setup complete. Start JARVIS with:  .\scripts\start.ps1' -ForegroundColor Green
