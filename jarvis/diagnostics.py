"""Environment inspection, run on the machine where JARVIS is started.

Reports hardware, runtimes, audio/video devices and which credentials are
present — never their values. Only fixed, read-only system queries are run.
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

IS_WINDOWS = sys.platform == "win32"

_PS_QUERY = r"""
$ErrorActionPreference = 'SilentlyContinue'
$os  = Get-CimInstance Win32_OperatingSystem
$cpu = Get-CimInstance Win32_Processor | Select-Object -First 1
$gpu = Get-CimInstance Win32_VideoController | ForEach-Object { [pscustomobject]@{ name = $_.Name; vram_mb = [math]::Round($_.AdapterRAM / 1MB) } }
$mic = Get-PnpDevice -Class AudioEndpoint -Status OK | Where-Object { $_.FriendlyName -match 'Microphone|Mikrofon|Input|Array' } | Select-Object -ExpandProperty FriendlyName
$cam = @(Get-PnpDevice -Class Camera -Status OK) + @(Get-PnpDevice -Class Image -Status OK) | Select-Object -ExpandProperty FriendlyName
[pscustomobject]@{
  os_caption = $os.Caption; os_build = $os.BuildNumber; cpu = $cpu.Name
  gpus = @($gpu); microphones = @($mic); cameras = @($cam)
} | ConvertTo-Json -Depth 4 -Compress
"""


def _memory_gb() -> float | None:
    try:
        if IS_WINDOWS:
            import ctypes

            class MEMORYSTATUSEX(ctypes.Structure):
                _fields_ = [
                    ("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("sullAvailExtendedVirtual", ctypes.c_ulonglong),
                ]

            stat = MEMORYSTATUSEX()
            stat.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat))
            return round(stat.ullTotalPhys / 1024**3, 1)
        with open("/proc/meminfo", encoding="ascii") as fh:
            for line in fh:
                if line.startswith("MemTotal:"):
                    return round(int(line.split()[1]) / 1024**2, 1)
    except Exception:
        return None
    return None


def _windows_details() -> dict[str, Any]:
    if not IS_WINDOWS:
        return {}
    try:
        out = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", _PS_QUERY],
            capture_output=True, text=True, timeout=15, check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return json.loads(out.stdout) if out.stdout.strip() else {}
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError):
        return {"error": "Windows device query failed"}


def _nvidia_gpus() -> list[dict[str, Any]]:
    """Exact GPU memory from nvidia-smi when an NVIDIA driver is installed."""
    exe = shutil.which("nvidia-smi")
    if not exe:
        return []
    try:
        out = subprocess.run(
            [exe, "--query-gpu=name,memory.total", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=10, check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    gpus = []
    for line in out.stdout.splitlines():
        name, _, mem = line.rpartition(",")
        if name.strip() and mem.strip().isdigit():
            gpus.append({"name": name.strip(), "vram_mb": int(mem.strip()), "source": "nvidia-smi"})
    return gpus


def _merge_gpus(wmi: list[dict[str, Any]], nvidia: list[dict[str, Any]]) -> list[dict[str, Any]]:
    # Windows reports AdapterRAM as a 32-bit value, so cards with more than
    # 4 GB show about 4095 MB. Prefer nvidia-smi's figure where it has one.
    exact = {g["name"]: g for g in nvidia}
    merged = [exact.pop(g.get("name"), {**g, "source": "windows (capped at 4 GB)"}) for g in wmi]
    return merged + list(exact.values())


def collect(data_dir: Path, credential_envs: dict[str, str]) -> dict[str, Any]:
    from .secrets import has_secret

    disk = shutil.disk_usage(data_dir if data_dir.exists() else Path.cwd())
    win = _windows_details()
    tools = {name: bool(shutil.which(name)) for name in ("git", "node", "ollama", "docker", "wsl", "winget")}
    return {
        "platform": {
            "system": platform.system(),
            "release": platform.release(),
            "version": platform.version(),
            "machine": platform.machine(),
            "os_caption": win.get("os_caption"),
            "os_build": win.get("os_build"),
        },
        "python": sys.version.split()[0],
        "cpu": {"name": win.get("cpu") or platform.processor() or None, "logical_cores": os.cpu_count()},
        "memory_gb": _memory_gb(),
        "disk_free_gb": round(disk.free / 1024**3, 1),
        "gpus": _merge_gpus(win.get("gpus") or [], _nvidia_gpus()),
        "microphones": win.get("microphones") or [],
        "cameras": win.get("cameras") or [],
        "devices_detectable": IS_WINDOWS,
        "tools_on_path": tools,
        "credentials_present": {label: has_secret(env) for label, env in credential_envs.items()},
    }
