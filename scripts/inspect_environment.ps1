<#
.SYNOPSIS
  Read-only inspection of this PC for JARVIS planning.
.DESCRIPTION
  Reports Windows version, CPU, RAM, GPU, disk, development tools, audio and
  video devices, and whether Ollama / Home Assistant are reachable. Reports
  only whether credentials are SET — never their values. Changes nothing.
.PARAMETER HomeAssistantUrl
  Optional URL to probe, e.g. http://homeassistant.local:8123
#>
param([string]$HomeAssistantUrl = 'http://homeassistant.local:8123')
$ErrorActionPreference = 'SilentlyContinue'

function Section($t) { Write-Host "`n== $t ==" -ForegroundColor Cyan }

Section 'Windows'
$os = Get-CimInstance Win32_OperatingSystem
"{0}  (build {1}, {2})" -f $os.Caption, $os.BuildNumber, $env:PROCESSOR_ARCHITECTURE

Section 'Hardware'
$cpu = Get-CimInstance Win32_Processor | Select-Object -First 1
"CPU : {0} ({1} cores / {2} threads)" -f $cpu.Name.Trim(), $cpu.NumberOfCores, $cpu.NumberOfLogicalProcessors
"RAM : {0:N1} GB" -f ($os.TotalVisibleMemorySize / 1MB)
Get-CimInstance Win32_VideoController | ForEach-Object { "GPU : {0} ({1:N1} GB reported)" -f $_.Name, ($_.AdapterRAM / 1GB) }
if (Get-Command nvidia-smi -ErrorAction SilentlyContinue) { nvidia-smi --query-gpu=name,memory.total --format=csv,noheader | ForEach-Object { "NVIDIA: $_" } }
Get-PSDrive -PSProvider FileSystem | Where-Object { $_.Used -ne $null } | ForEach-Object { "Disk {0}: {1:N0} GB free of {2:N0} GB" -f $_.Name, ($_.Free / 1GB), (($_.Used + $_.Free) / 1GB) }

Section 'Tools'
foreach ($t in 'python', 'py', 'git', 'node', 'ollama', 'docker', 'wsl', 'winget') {
    $c = Get-Command $t -ErrorAction SilentlyContinue
    "{0,-8} {1}" -f $t, $(if ($c) { $c.Source } else { '-' })
}
try { "Python version: " + (& py -3 --version 2>&1) } catch { }

Section 'Audio input devices'
$mics = Get-PnpDevice -Class AudioEndpoint -Status OK | Where-Object { $_.FriendlyName -match 'Microphone|Mikrofon|Input|Array' }
if ($mics) { $mics | ForEach-Object { "- " + $_.FriendlyName } } else { '(none detected)' }

Section 'Cameras'
$cams = @(Get-PnpDevice -Class Camera -Status OK) + @(Get-PnpDevice -Class Image -Status OK)
if ($cams) { $cams | ForEach-Object { "- " + $_.FriendlyName } } else { '(none detected)' }

Section 'Local services'
try {
    $tags = Invoke-RestMethod -Uri 'http://127.0.0.1:11434/api/tags' -TimeoutSec 3
    "Ollama: running; models: " + (($tags.models | ForEach-Object { $_.name }) -join ', ')
} catch { 'Ollama: not reachable on 127.0.0.1:11434' }
try {
    $r = Invoke-WebRequest -Uri "$HomeAssistantUrl/api/" -TimeoutSec 4 -UseBasicParsing
    "Home Assistant: reachable at $HomeAssistantUrl (HTTP $($r.StatusCode))"
} catch {
    $code = $_.Exception.Response.StatusCode.value__
    if ($code -eq 401) { "Home Assistant: reachable at $HomeAssistantUrl (needs token)" } else { "Home Assistant: not reachable at $HomeAssistantUrl" }
}

Section 'Credentials (presence only)'
$envFile = Join-Path (Split-Path -Parent $PSScriptRoot) '.env'
foreach ($name in 'ANTHROPIC_API_KEY', 'HA_TOKEN') {
    $set = [bool][Environment]::GetEnvironmentVariable($name)
    if (-not $set -and (Test-Path $envFile)) { $set = [bool](Select-String -Path $envFile -Pattern "^$name=.+" -Quiet) }
    "{0,-18} {1}" -f $name, $(if ($set) { 'set' } else { 'not set' })
}
