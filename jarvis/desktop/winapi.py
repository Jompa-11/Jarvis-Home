"""Small Windows API helpers (ctypes, user level): monitors and the AppUserModelID."""

from __future__ import annotations

import logging
import sys
from dataclasses import dataclass

log = logging.getLogger(__name__)

MONITORINFOF_PRIMARY = 0x1


@dataclass(frozen=True)
class Monitor:
    left: int
    top: int
    right: int
    bottom: int
    # Work area: the monitor without the taskbar.
    work_left: int
    work_top: int
    work_right: int
    work_bottom: int
    primary: bool

    @property
    def work_width(self) -> int:
        return self.work_right - self.work_left

    @property
    def work_height(self) -> int:
        return self.work_bottom - self.work_top


def pick_primary(monitors: list[Monitor]) -> Monitor | None:
    """The primary monitor (flagged by Windows); else the one at the origin; else the first."""
    if not monitors:
        return None
    flagged = [m for m in monitors if m.primary]
    if flagged:
        return flagged[0]
    at_origin = [m for m in monitors if m.left <= 0 < m.right and m.top <= 0 < m.bottom]
    return (at_origin or monitors)[0]


def window_rect_on(monitor: Monitor, width: int = 1480, height: int = 920) -> tuple[int, int, int, int]:
    """(x, y, w, h) for a window that lies inside the monitor's work area, so maximizing it
    maximizes it on THAT monitor."""
    w = min(width, max(640, monitor.work_width - 80))
    h = min(height, max(480, monitor.work_height - 80))
    x = monitor.work_left + max(0, (monitor.work_width - w) // 2)
    y = monitor.work_top + max(0, (monitor.work_height - h) // 2)
    return x, y, w, h


def list_monitors() -> list[Monitor]:  # pragma: no cover - Windows only
    if sys.platform != "win32":
        return []
    import ctypes
    from ctypes import wintypes

    class MONITORINFO(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT), ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD)]

    user32 = ctypes.windll.user32
    out: list[Monitor] = []
    proc_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HMONITOR, wintypes.HDC, ctypes.POINTER(wintypes.RECT), wintypes.LPARAM)

    def callback(hmon, _hdc, _rect, _lparam):
        info = MONITORINFO()
        info.cbSize = ctypes.sizeof(MONITORINFO)
        if user32.GetMonitorInfoW(hmon, ctypes.byref(info)):
            r, w = info.rcMonitor, info.rcWork
            out.append(Monitor(r.left, r.top, r.right, r.bottom, w.left, w.top, w.right, w.bottom, bool(info.dwFlags & MONITORINFOF_PRIMARY)))
        return True

    user32.EnumDisplayMonitors(None, None, proc_type(callback), 0)
    return out


def primary_monitor() -> Monitor | None:
    try:
        return pick_primary(list_monitors())
    except Exception:  # never stop the app because of a monitor query
        log.exception("Could not read the monitors")
        return None


def set_app_id(app_id: str) -> None:  # pragma: no cover - Windows only
    """Group this process's windows under the same taskbar button as the pinned shortcut."""
    if sys.platform != "win32":
        return
    import ctypes

    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(ctypes.c_wchar_p(app_id))
    except Exception:
        log.exception("Could not set the AppUserModelID")


def enable_dpi_awareness() -> None:  # pragma: no cover - Windows only
    """Use real pixels, so monitor coordinates and window positions agree on scaled displays."""
    if sys.platform != "win32":
        return
    import ctypes

    try:
        ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))  # PER_MONITOR_AWARE_V2
    except Exception:
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception:
            pass


def maximize_on(hwnd: int, monitor: Monitor) -> None:  # pragma: no cover - Windows only
    """Move the window into the monitor's work area (physical pixels), then maximize it there."""
    if sys.platform != "win32" or not hwnd:
        return
    import ctypes

    user32 = ctypes.windll.user32
    x, y, w, h = window_rect_on(monitor)
    SW_RESTORE, SW_MAXIMIZE = 9, 3
    user32.ShowWindow(hwnd, SW_RESTORE)  # a maximized window must be restored before it can move
    user32.SetWindowPos(hwnd, None, x, y, w, h, 0x0004 | 0x0010)  # SWP_NOZORDER | SWP_NOACTIVATE
    user32.ShowWindow(hwnd, SW_MAXIMIZE)


def bring_to_front(hwnd: int) -> None:  # pragma: no cover - Windows only
    if sys.platform != "win32" or not hwnd:
        return
    import ctypes

    ctypes.windll.user32.SetForegroundWindow(hwnd)


def allow_foreground(pid: int) -> None:  # pragma: no cover - Windows only
    """Let the running JARVIS (pid) take the foreground: this process was just started by a click."""
    if sys.platform != "win32" or not pid:
        return
    import ctypes

    ctypes.windll.user32.AllowSetForegroundWindow(int(pid))
