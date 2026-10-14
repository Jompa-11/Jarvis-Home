"""Restricted Windows desktop control.

Phase 1 scope, deliberately narrow:

* Launch applications from an explicit allowlist (exact command lines from the
  config file — the model only chooses an allowlist *key*, never a path or command).
* List visible top-level windows (titles can be personal, so the output is
  classified ``personal`` and is withheld from cloud models by default).
* Bring a window to the foreground.

There is no shell execution, no arbitrary file access, no keyboard or mouse
injection and no elevation. Every action supports dry-run.
"""

from __future__ import annotations

import logging
import subprocess
import sys
from dataclasses import dataclass
from typing import Any

from ..config import ComputerConfig

log = logging.getLogger(__name__)

IS_WINDOWS = sys.platform == "win32"


def is_windows() -> bool:
    """Injectable in tests, so the suite is green on Linux too."""
    return sys.platform == "win32"


class ComputerError(Exception):
    pass


@dataclass
class WindowInfo:
    handle: int
    title: str
    process_id: int


class WindowsControl:
    def __init__(self, cfg: ComputerConfig) -> None:
        self.cfg = cfg

    @property
    def supported(self) -> bool:
        return IS_WINDOWS

    @property
    def enabled(self) -> bool:
        """Whether the computer-control tools should be registered at all."""
        return self.supported if self.cfg.enabled is None else self.cfg.enabled

    def status(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "platform_supported": self.supported,
            "platform": sys.platform,
            "allowed_apps": sorted(self.cfg.allowed_apps),
        }

    def list_apps(self) -> list[dict[str, str]]:
        return [{"app": k, "description": v.description} for k, v in sorted(self.cfg.allowed_apps.items())]

    def open_app(self, app: str, *, dry_run: bool) -> dict[str, Any]:
        key = app.strip().lower()
        entry = self.cfg.allowed_apps.get(key)
        if entry is None:
            raise ComputerError(f"'{app}' is not an approved application. Approved: {', '.join(sorted(self.cfg.allowed_apps))}.")
        if dry_run:
            return {"app": key, "would_run": entry.command}
        if not self.supported:
            raise ComputerError("Opening applications is only supported on Windows.")
        flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        # A fixed argument list with shell=False: nothing from the model reaches a shell.
        proc = subprocess.Popen(  # noqa: S603 - command comes from the local allowlist only
            entry.command,
            shell=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=flags,
            close_fds=True,
        )
        log.info("Launched approved app %s (pid %s)", key, proc.pid)
        return {"app": key, "launched": True, "pid": proc.pid}

    def list_windows(self) -> list[WindowInfo]:
        if not self.supported:
            raise ComputerError("Window listing is only supported on Windows.")
        return _enum_windows()

    def focus_window(self, title_contains: str, *, dry_run: bool) -> dict[str, Any]:
        q = title_contains.strip().lower()
        if not q:
            raise ComputerError("A window title is required.")
        if not self.supported:
            if dry_run:
                return {"would_focus_title_containing": title_contains, "note": "Window matching is only available on Windows."}
            raise ComputerError("Window focus is only supported on Windows.")
        matches = [w for w in _enum_windows() if q in w.title.lower()]
        if not matches:
            raise ComputerError(f"No open window title contains '{title_contains}'.")
        if len(matches) > 1:
            raise ComputerError(f"{len(matches)} windows match '{title_contains}': " + "; ".join(w.title for w in matches[:8]))
        target = matches[0]
        if dry_run:
            return {"would_focus": target.title}
        ok = _set_foreground(target.handle)
        return {"focused": target.title, "verified": ok}


# --- Win32 helpers (ctypes, no extra dependencies) ---------------------------------

def _enum_windows() -> list[WindowInfo]:  # pragma: no cover - Windows only
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    results: list[WindowInfo] = []
    EnumProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def callback(hwnd, _lparam):
        if not user32.IsWindowVisible(hwnd):
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        if length == 0:
            return True
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, length + 1)
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        results.append(WindowInfo(handle=int(hwnd), title=buf.value, process_id=int(pid.value)))
        return True

    user32.EnumWindows(EnumProc(callback), 0)
    return results


def _set_foreground(hwnd: int) -> bool:  # pragma: no cover - Windows only
    import ctypes

    user32 = ctypes.windll.user32
    SW_RESTORE = 9
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, SW_RESTORE)
    user32.SetForegroundWindow(hwnd)
    return int(user32.GetForegroundWindow()) == int(hwnd)
