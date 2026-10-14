"""Desktop control: volume, media keys, typing, shortcuts, clipboard, windows, power.

The rules live here; the actual Windows calls are in
:mod:`jarvis.computer.winapi_desktop`, which tests replace with a fake.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from .winapi_desktop import (
    VK_MEDIA_NEXT,
    VK_MEDIA_PLAY_PAUSE,
    VK_MEDIA_PREV,
    VK_MEDIA_STOP,
    WindowsDesktopAPI,
)
from .windows import ComputerError, WindowInfo

log = logging.getLogger(__name__)

MAX_TYPE_CHARS = 2000
MAX_CLIPBOARD_CHARS = 10000

MODIFIERS = {"ctrl": 0x11, "control": 0x11, "alt": 0x12, "shift": 0x10, "win": 0x5B, "windows": 0x5B, "cmd": 0x5B}
NAMED_KEYS = {
    "enter": 0x0D, "return": 0x0D, "tab": 0x09, "esc": 0x1B, "escape": 0x1B, "space": 0x20, "mellanslag": 0x20,
    "backspace": 0x08, "delete": 0x2E, "del": 0x2E, "insert": 0x2D, "home": 0x24, "end": 0x23,
    "pageup": 0x21, "pagedown": 0x22, "up": 0x26, "down": 0x28, "left": 0x25, "right": 0x27,
    "printscreen": 0x2C, "capslock": 0x14, "plus": 0xBB, "minus": 0xBD, "comma": 0xBC, "period": 0xBE,
}
NAMED_KEYS.update({f"f{i}": 0x6F + i for i in range(1, 25)})
BLOCKED_COMBOS = {
    frozenset({"win", "r"}): "Kör-dialogen",
    frozenset({"win", "x"}): "snabbmenyn med PowerShell",
}
# A window that takes typed text as commands. Typing or pressing Enter there is HIGH risk.
CONSOLE_CLASSES = {"consolewindowclass", "cascadia_hosting_window_class", "virtualconsoleclass", "pseudoconsolewindowclass"}
CONSOLE_PROCESSES = {"powershell.exe", "pwsh.exe", "cmd.exe", "windowsterminal.exe", "wt.exe", "openconsole.exe",
                     "conhost.exe", "bash.exe", "wsl.exe", "python.exe", "py.exe", "powershell_ise.exe"}
RUN_DIALOG_TITLES = {"kör", "run"}
MEDIA_KEYS = {"play": VK_MEDIA_PLAY_PAUSE, "pause": VK_MEDIA_PLAY_PAUSE, "playpause": VK_MEDIA_PLAY_PAUSE,
              "next": VK_MEDIA_NEXT, "previous": VK_MEDIA_PREV, "stop": VK_MEDIA_STOP}
WINDOW_ACTIONS = ("focus", "minimize", "maximize", "restore", "close")


def parse_shortcut(combo: str) -> list[int]:
    """'ctrl+shift+t' -> virtual-key codes. Refuses Ctrl+Alt+Del."""
    parts = [p.strip().lower() for p in re.split(r"[+\s]+", combo.strip()) if p.strip()]
    if not parts:
        raise ComputerError("Ange ett kortkommando, till exempel 'ctrl+s'.")
    if len(parts) > 5:
        raise ComputerError("Högst 5 tangenter i taget.")
    names = set(parts)
    if {"ctrl", "control"} & names and "alt" in names and {"del", "delete"} & names:
        raise ComputerError("Ctrl+Alt+Del kan inte skickas av JARVIS. Tryck det själv på tangentbordet.")
    canonical = {"control": "ctrl", "windows": "win", "cmd": "win"}
    folded = {canonical.get(p, p) for p in parts}
    for combo, what in BLOCKED_COMBOS.items():
        if combo <= folded:
            raise ComputerError(f"JARVIS trycker aldrig {'+'.join(sorted(combo))}; det öppnar {what}. Gör det själv.")
    vks: list[int] = []
    for part in parts:
        if part in MODIFIERS:
            vks.append(MODIFIERS[part])
        elif part in NAMED_KEYS:
            vks.append(NAMED_KEYS[part])
        elif len(part) == 1 and part.isalnum():
            vks.append(ord(part.upper()))
        else:
            raise ComputerError(f"Okänd tangent '{part}'.")
    return vks


class Desktop:
    def __init__(self, api: Any | None = None) -> None:
        self.api = api or WindowsDesktopAPI()

    def _require_windows(self, what: str) -> None:
        if not self.api.supported:
            raise ComputerError(f"{what} fungerar bara på Windows.")

    # -- volume and media ----------------------------------------------------
    def volume_status(self) -> dict[str, Any]:
        self._require_windows("Volymen")
        level, muted = self.api.get_volume()
        return {"volume": level, "muted": muted}

    def set_volume(self, percent: int, *, dry_run: bool) -> dict[str, Any]:
        percent = max(0, min(100, int(percent)))
        if dry_run:
            return {"would_set_volume": percent}
        self._require_windows("Volymen")
        self.api.set_volume(percent)
        level, muted = self.api.get_volume()
        return {"volume": level, "muted": muted, "verified": abs(level - percent) <= 2}

    def adjust_volume(self, step: int, *, dry_run: bool) -> dict[str, Any]:
        self._require_windows("Volymen")
        current, _ = self.api.get_volume()
        target = max(0, min(100, current + int(step)))
        if dry_run:
            return {"volume": current, "would_set_volume": target}
        self.api.set_volume(target)
        level, muted = self.api.get_volume()
        return {"volume": level, "muted": muted, "verified": abs(level - target) <= 2}

    def set_mute(self, muted: bool | None, *, dry_run: bool) -> dict[str, Any]:
        self._require_windows("Volymen")
        level, now_muted = self.api.get_volume()
        target = (not now_muted) if muted is None else bool(muted)
        if dry_run:
            return {"muted": now_muted, "would_set_muted": target}
        self.api.set_mute(target)
        level, after = self.api.get_volume()
        return {"volume": level, "muted": after, "verified": after == target}

    def media(self, action: str, *, dry_run: bool) -> dict[str, Any]:
        key = MEDIA_KEYS.get(action.strip().lower())
        if key is None:
            raise ComputerError(f"Okänd mediahandling '{action}'.")
        if dry_run:
            return {"would_press": action}
        self._require_windows("Mediatangenterna")
        self.api.press_keys([key])
        return {"pressed": action, "verified": None}

    # -- typing and shortcuts ------------------------------------------------
    def type_text(self, text: str, *, dry_run: bool) -> dict[str, Any]:
        if not text:
            raise ComputerError("Ingen text att skriva.")
        if len(text) > MAX_TYPE_CHARS:
            raise ComputerError(f"Högst {MAX_TYPE_CHARS} tecken åt gången.")
        window = self.active_window_title()
        if dry_run:
            return {"would_type_characters": len(text), "into_window": window}
        self._require_windows("Att skriva text")
        self.api.type_text(text)
        return {"typed_characters": len(text), "into_window": window}

    def press(self, combo: str, *, dry_run: bool) -> dict[str, Any]:
        vks = parse_shortcut(combo)
        window = self.active_window_title()
        if dry_run:
            return {"would_press": combo, "into_window": window}
        self._require_windows("Kortkommandon")
        self.api.press_keys(vks)
        return {"pressed": combo, "into_window": window}

    def active_window_title(self) -> str | None:
        try:
            return self.api.active_window_title()
        except Exception:
            return None

    def active_window_kind(self) -> dict[str, str]:
        try:
            return self.api.active_window_kind()
        except Exception:
            return {"title": "", "class_name": "", "process": ""}

    def typing_is_dangerous(self) -> str | None:
        """What makes typing into the window in front risky, or None.

        A terminal, a console or the Run dialog turns typed text into commands, so
        those always need the owner's confirmation. If the window cannot be read,
        JARVIS treats it as risky rather than guessing.
        """
        kind = self.active_window_kind()
        title, cls, proc = kind.get("title", ""), (kind.get("class_name") or "").lower(), (kind.get("process") or "").lower()
        if title.strip().lower() in RUN_DIALOG_TITLES or (cls == "#32770" and proc == "explorer.exe"):
            return "Kör-dialogen"
        if cls in CONSOLE_CLASSES or proc in CONSOLE_PROCESSES:
            return f"ett kommandofönster ({proc or cls})"
        if not proc and not cls:
            return "ett okänt fönster"
        return None

    # -- clipboard -----------------------------------------------------------
    def read_clipboard(self) -> dict[str, Any]:
        self._require_windows("Urklipp")
        text = self.api.get_clipboard() or ""
        clipped = len(text) > MAX_CLIPBOARD_CHARS
        return {"text": text[:MAX_CLIPBOARD_CHARS], "characters": len(text), "truncated": clipped}

    def write_clipboard(self, text: str, *, dry_run: bool) -> dict[str, Any]:
        if len(text) > MAX_CLIPBOARD_CHARS:
            raise ComputerError(f"Högst {MAX_CLIPBOARD_CHARS} tecken till urklipp.")
        if dry_run:
            return {"would_copy_characters": len(text)}
        self._require_windows("Urklipp")
        self.api.set_clipboard(text)
        return {"copied_characters": len(text)}

    # -- windows -------------------------------------------------------------
    def windows(self) -> list[WindowInfo]:
        self._require_windows("Fönsterlistan")
        return self.api.list_windows()

    def window(self, title_contains: str, action: str, *, dry_run: bool) -> dict[str, Any]:
        action = action.strip().lower()
        if action not in WINDOW_ACTIONS:
            raise ComputerError(f"Okänd fönsterhandling '{action}'.")
        q = title_contains.strip().lower()
        if not q:
            raise ComputerError("Ange en del av fönstrets titel.")
        if dry_run and not self.api.supported:
            return {"would": action, "window": title_contains}
        matches = [w for w in self.windows() if q in w.title.lower()]
        if not matches:
            raise ComputerError(f"Inget öppet fönster har '{title_contains}' i titeln.")
        if len(matches) > 1:
            titles = "; ".join(w.title for w in matches[:6])
            raise ComputerError(f"{len(matches)} fönster matchar '{title_contains}': {titles}. Vilket menar du?")
        target = matches[0]
        if dry_run:
            return {"would": action, "window": target.title}
        if action == "focus":
            from .windows import _set_foreground

            return {"window": target.title, "action": action, "verified": _set_foreground(target.handle)}
        self.api.window_action(target.handle, action)
        return {"window": target.title, "action": action, "verified": None}

    # -- power ---------------------------------------------------------------
    def lock(self, *, dry_run: bool) -> dict[str, Any]:
        if dry_run:
            return {"would": "lock"}
        self._require_windows("Att låsa datorn")
        self.api.lock_workstation()
        return {"locked": True}

    def power(self, action: str, *, dry_run: bool) -> dict[str, Any]:
        action = action.strip().lower()
        if action not in ("shutdown", "restart", "sleep", "logout"):
            raise ComputerError(f"Okänd handling '{action}'.")
        if dry_run:
            return {"would": action}
        self._require_windows("Av- och omstart")
        self.api.power(action)
        return {"action": action, "started": True}
