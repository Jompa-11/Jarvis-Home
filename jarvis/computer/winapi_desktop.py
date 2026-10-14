"""Raw Windows calls for desktop control (ctypes only, no extra dependencies).

Everything that touches a real Windows API lives here, behind small methods, so
the logic in :mod:`jarvis.computer.desktop` can be tested with a fake.
Nothing here is elevated and nothing runs a shell.
"""

from __future__ import annotations

import ctypes
import logging
import subprocess
import sys
import uuid
from ctypes import wintypes

log = logging.getLogger(__name__)

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# Virtual-key codes used for media and volume keys.
VK_VOLUME_MUTE, VK_VOLUME_DOWN, VK_VOLUME_UP = 0xAD, 0xAE, 0xAF
VK_MEDIA_NEXT, VK_MEDIA_PREV, VK_MEDIA_STOP, VK_MEDIA_PLAY_PAUSE = 0xB0, 0xB1, 0xB2, 0xB3

CF_UNICODETEXT = 13
SW_MINIMIZE, SW_MAXIMIZE, SW_RESTORE = 6, 3, 9
WM_CLOSE = 0x0010
KEYEVENTF_KEYUP, KEYEVENTF_UNICODE = 0x0002, 0x0004
INPUT_KEYBOARD = 1


class _GUID(ctypes.Structure):
    _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD), ("Data3", wintypes.WORD), ("Data4", ctypes.c_ubyte * 8)]

    @classmethod
    def from_string(cls, text: str) -> "_GUID":
        u = uuid.UUID(text)
        return cls(u.time_low, u.time_mid, u.time_hi_version, (ctypes.c_ubyte * 8).from_buffer_copy(u.bytes[8:]))


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD), ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong))]


class _INPUT(ctypes.Structure):
    class _U(ctypes.Union):
        _fields_ = [("ki", _KEYBDINPUT), ("padding", ctypes.c_ubyte * 32)]

    _anonymous_ = ("u",)
    _fields_ = [("type", wintypes.DWORD), ("u", _U)]


def _send_inputs(inputs: list[_INPUT]) -> None:  # pragma: no cover - real input injection
    array = (_INPUT * len(inputs))(*inputs)
    ctypes.windll.user32.SendInput(len(inputs), ctypes.byref(array), ctypes.sizeof(_INPUT))


def _key_input(vk: int, up: bool) -> _INPUT:
    return _INPUT(type=INPUT_KEYBOARD, ki=_KEYBDINPUT(wVk=vk, wScan=0, dwFlags=KEYEVENTF_KEYUP if up else 0, time=0, dwExtraInfo=None))


def _char_input(ch: str, up: bool) -> _INPUT:
    flags = KEYEVENTF_UNICODE | (KEYEVENTF_KEYUP if up else 0)
    return _INPUT(type=INPUT_KEYBOARD, ki=_KEYBDINPUT(wVk=0, wScan=ord(ch), dwFlags=flags, time=0, dwExtraInfo=None))


def _vcall(ptr: ctypes.c_void_p, index: int, restype, *argtypes):  # pragma: no cover - COM plumbing
    vtable = ctypes.cast(ptr, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    return ctypes.WINFUNCTYPE(restype, ctypes.c_void_p, *argtypes)(vtable[index])


def _release(ptr: ctypes.c_void_p) -> None:  # pragma: no cover - COM plumbing
    if ptr:
        _vcall(ptr, 2, ctypes.c_ulong)(ptr)


class _EndpointVolume:  # pragma: no cover - needs a real audio device
    """The default playback device's volume, through IAudioEndpointVolume."""

    CLSID_MMDeviceEnumerator = "{BCDE0395-E52F-467C-8E3D-C4579291692E}"
    IID_IMMDeviceEnumerator = "{A95664D2-9614-4F35-A746-DE8DB63617E6}"
    IID_IAudioEndpointVolume = "{5CDF2C82-841E-4546-9722-0CF74078229A}"

    def __enter__(self) -> "_EndpointVolume":
        ole32 = ctypes.windll.ole32
        ole32.CoInitializeEx(None, 0x2)  # apartment threaded; S_FALSE when already initialised
        self._enumerator = ctypes.c_void_p()
        self._device = ctypes.c_void_p()
        self.ptr = ctypes.c_void_p()
        clsid, iid_enum = _GUID.from_string(self.CLSID_MMDeviceEnumerator), _GUID.from_string(self.IID_IMMDeviceEnumerator)
        hr = ole32.CoCreateInstance(ctypes.byref(clsid), None, 0x17, ctypes.byref(iid_enum), ctypes.byref(self._enumerator))
        if hr != 0:
            raise OSError(f"CoCreateInstance failed (0x{hr & 0xFFFFFFFF:08X})")
        get_default = _vcall(self._enumerator, 4, ctypes.HRESULT, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p))
        get_default(self._enumerator, 0, 1, ctypes.byref(self._device))  # eRender, eMultimedia
        iid_vol = _GUID.from_string(self.IID_IAudioEndpointVolume)
        activate = _vcall(self._device, 3, ctypes.HRESULT, ctypes.POINTER(_GUID), wintypes.DWORD, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p))
        activate(self._device, ctypes.byref(iid_vol), 0x17, None, ctypes.byref(self.ptr))
        return self

    def __exit__(self, *exc) -> None:
        for p in (self.ptr, self._device, self._enumerator):
            _release(p)

    def get(self) -> tuple[int, bool]:
        level = ctypes.c_float()
        _vcall(self.ptr, 9, ctypes.HRESULT, ctypes.POINTER(ctypes.c_float))(self.ptr, ctypes.byref(level))
        muted = wintypes.BOOL()
        _vcall(self.ptr, 15, ctypes.HRESULT, ctypes.POINTER(wintypes.BOOL))(self.ptr, ctypes.byref(muted))
        return round(level.value * 100), bool(muted.value)

    def set_level(self, percent: int) -> None:
        _vcall(self.ptr, 7, ctypes.HRESULT, ctypes.c_float, ctypes.c_void_p)(self.ptr, percent / 100.0, None)

    def set_mute(self, muted: bool) -> None:
        _vcall(self.ptr, 14, ctypes.HRESULT, wintypes.BOOL, ctypes.c_void_p)(self.ptr, bool(muted), None)


class WindowsDesktopAPI:
    """The real implementation. Methods raise OSError when Windows refuses."""

    @property
    def supported(self) -> bool:
        return sys.platform == "win32"

    # -- volume --------------------------------------------------------------
    def get_volume(self) -> tuple[int, bool]:  # pragma: no cover - needs a real audio device
        with _EndpointVolume() as vol:
            return vol.get()

    def set_volume(self, percent: int) -> None:  # pragma: no cover - needs a real audio device
        with _EndpointVolume() as vol:
            vol.set_level(percent)

    def set_mute(self, muted: bool) -> None:  # pragma: no cover - needs a real audio device
        with _EndpointVolume() as vol:
            vol.set_mute(muted)

    # -- keyboard ------------------------------------------------------------
    def press_keys(self, vks: list[int]) -> None:  # pragma: no cover - real input injection
        _send_inputs([_key_input(vk, False) for vk in vks] + [_key_input(vk, True) for vk in reversed(vks)])

    def type_text(self, text: str) -> None:  # pragma: no cover - real input injection
        batch: list[_INPUT] = []
        for ch in text:
            batch += [_char_input(ch, False), _char_input(ch, True)]
            if len(batch) >= 200:  # SendInput takes a bounded array at a time
                _send_inputs(batch)
                batch = []
        if batch:
            _send_inputs(batch)

    # -- clipboard -----------------------------------------------------------
    def get_clipboard(self) -> str:  # pragma: no cover - real clipboard
        user32, kernel32 = ctypes.windll.user32, ctypes.windll.kernel32
        if not user32.OpenClipboard(None):
            raise OSError("Kunde inte öppna urklipp.")
        try:
            handle = user32.GetClipboardData(CF_UNICODETEXT)
            if not handle:
                return ""
            kernel32.GlobalLock.restype = ctypes.c_void_p
            ptr = kernel32.GlobalLock(handle)
            try:
                return ctypes.c_wchar_p(ptr).value or ""
            finally:
                kernel32.GlobalUnlock(handle)
        finally:
            user32.CloseClipboard()

    def set_clipboard(self, text: str) -> None:  # pragma: no cover - real clipboard
        user32, kernel32 = ctypes.windll.user32, ctypes.windll.kernel32
        if not user32.OpenClipboard(None):
            raise OSError("Kunde inte öppna urklipp.")
        try:
            user32.EmptyClipboard()
            size = (len(text) + 1) * ctypes.sizeof(ctypes.c_wchar)
            kernel32.GlobalAlloc.restype = ctypes.c_void_p
            handle = kernel32.GlobalAlloc(0x0042, size)  # GMEM_MOVEABLE | GMEM_ZEROINIT
            kernel32.GlobalLock.restype = ctypes.c_void_p
            ptr = kernel32.GlobalLock(handle)
            ctypes.memmove(ptr, ctypes.create_unicode_buffer(text), size)
            kernel32.GlobalUnlock(handle)
            user32.SetClipboardData(CF_UNICODETEXT, ctypes.c_void_p(handle))
        finally:
            user32.CloseClipboard()

    # -- windows -------------------------------------------------------------
    def window_action(self, hwnd: int, action: str) -> None:  # pragma: no cover - real windows
        user32 = ctypes.windll.user32
        if action == "close":
            user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)
        elif action == "minimize":
            user32.ShowWindow(hwnd, SW_MINIMIZE)
        elif action == "maximize":
            user32.ShowWindow(hwnd, SW_MAXIMIZE)
        elif action == "restore":
            user32.ShowWindow(hwnd, SW_RESTORE)

    def active_window_title(self) -> str | None:  # pragma: no cover - real windows
        user32 = ctypes.windll.user32
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return None
        length = user32.GetWindowTextLengthW(hwnd)
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, length + 1)
        return buf.value or None

    # -- power ---------------------------------------------------------------
    def lock_workstation(self) -> None:  # pragma: no cover - really locks the PC
        ctypes.windll.user32.LockWorkStation()

    def power(self, action: str) -> None:  # pragma: no cover - really shuts the PC down
        if action == "sleep":
            ctypes.windll.powrprof.SetSuspendState(False, False, False)
            return
        flag = {"shutdown": "/s", "restart": "/r", "logout": "/l"}[action]
        args = ["shutdown.exe", flag] + ([] if flag == "/l" else ["/t", "0"])
        subprocess.Popen(args, creationflags=_NO_WINDOW, close_fds=True)  # noqa: S603 - fixed arguments
