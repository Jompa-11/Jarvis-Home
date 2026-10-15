"""Find and start installed programs.

Windows lists every installed program, including Microsoft Store apps, through
``Get-StartApps`` (name + AppID). JARVIS reads that list once, caches it, and
starts a program by handing Explorer the AppID it found itself:

    explorer.exe shell:AppsFolder\\<AppID>

The model never supplies a path or a command line. It supplies a search word,
which is matched against the cached list; only an AppID that came from Windows
is ever launched. Names in ``computer.allowed_apps`` keep working as aliases
with their own fixed command line.
"""

from __future__ import annotations

import difflib
import json
import logging
import subprocess
import sys
import time
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from ..config import ComputerConfig
from .windows import is_windows as _is_windows
from .windows import list_windows as _list_windows
from .windows import set_foreground as _focus_window

log = logging.getLogger(__name__)

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
# Our own fixed command. The UTF-8 prelude is needed because Windows PowerShell
# otherwise writes program names in the ANSI code page, which cannot hold every name.
LIST_COMMAND = [
    "powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
    "[Console]::OutputEncoding=[Text.UTF8Encoding]::new(); "
    "Get-StartApps | Select-Object Name,AppID | ConvertTo-Json -Compress",
]
CACHE_SECONDS = 300.0
MAX_CHOICES = 6
# Below this a fuzzy match is not a match at all.
FUZZY_MIN = 0.55
# A fuzzy winner must be this much better than the runner-up to be used without asking.
FUZZY_MARGIN = 0.12
# How long to wait for a window after starting a program, before saying so.
WINDOW_WAIT_S = 8.0


@dataclass(frozen=True)
class App:
    name: str
    app_id: str


def _fold(text: str) -> str:
    """Lower-case and strip accents, so 'Anteckningar' matches 'anteckningar'."""
    return "".join(c for c in unicodedata.normalize("NFKD", text.lower()) if not unicodedata.combining(c)).strip()


def read_start_apps() -> list[App]:  # pragma: no cover - runs a real PowerShell query
    raw = subprocess.run(LIST_COMMAND, capture_output=True, timeout=30, creationflags=_NO_WINDOW).stdout or b""
    out = raw.decode("utf-8", errors="replace")
    data = json.loads(out) if out.strip() else []
    if isinstance(data, dict):
        data = [data]
    return [App(str(d["Name"]), str(d["AppID"])) for d in data if d.get("Name") and d.get("AppID")]


def _squeeze(text: str) -> str:
    """Folded and without spaces, so "cap cut" and "CapCut" compare equal."""
    return "".join(_fold(text).split())


def similarity(query: str, name: str) -> float:
    """0-1. Compares both as written and with the spaces removed."""
    q, n = _fold(query), _fold(name)
    direct = difflib.SequenceMatcher(None, q, n).ratio()
    squeezed = difflib.SequenceMatcher(None, _squeeze(query), _squeeze(name)).ratio()
    return max(direct, squeezed)


def rank(query: str, apps: list[App]) -> list[tuple[float, int, App]]:
    """(score, app) best first. Exact and prefix matches first, then a fuzzy match,
    so a misheard or misspelt name ("cap cut", "cicaput") still finds CapCut."""
    q, qs = _fold(query), _squeeze(query)
    if not q:
        return []
    words = [w for w in q.split() if w]
    scored: list[tuple[float, int, App]] = []
    for app in apps:
        name, ns = _fold(app.name), _squeeze(app.name)
        if name == q or ns == qs:
            tier, score = 0, 1.0
        elif name.startswith(q) or ns.startswith(qs):
            tier, score = 1, 0.98
        elif q in name or qs in ns:
            tier, score = 2, 0.95
        elif words and all(w in name for w in words):
            tier, score = 3, 0.9
        else:
            score = similarity(query, app.name)
            if score < FUZZY_MIN:
                continue
            tier = 4
        scored.append((score, tier, app))
    scored.sort(key=lambda s: (s[1], -s[0], len(s[2].name), s[2].name))
    return scored


class AppLauncher:
    """The Start-menu index plus the allowlist aliases from the config."""

    def __init__(
        self,
        cfg: ComputerConfig,
        *,
        list_apps: Callable[[], list[App]] = read_start_apps,
        launch: Callable[[list[str]], None] | None = None,
        is_windows: Callable[[], bool] | None = None,
        windows: Callable[[], list] | None = None,
        focus: Callable[[int], bool] | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.cfg = cfg
        self._list_apps = list_apps
        self._launch = launch or _launch_detached
        self.is_windows = is_windows or _is_windows
        self._windows = windows or _list_windows
        self._focus = focus or _focus_window
        self.clock = clock
        self._sleep = sleep
        self._apps: list[App] = []
        self._read_at: float | None = None
        self.last_error: str | None = None

    # -- the index -----------------------------------------------------------
    def apps(self, *, force: bool = False) -> list[App]:
        if force or self._read_at is None or self.clock() - self._read_at > CACHE_SECONDS:
            try:
                self._apps = self._list_apps()
                self.last_error = None
            except Exception as exc:  # a missing or slow PowerShell must not break JARVIS
                self.last_error = f"Could not read the list of installed programs ({type(exc).__name__})"
                log.warning("Get-StartApps failed: %s", type(exc).__name__)
            self._read_at = self.clock()
        return self._apps

    def aliases(self) -> dict[str, Any]:
        return self.cfg.allowed_apps

    def list_public(self, limit: int = 60) -> dict[str, Any]:
        apps = self.apps()
        return {
            "aliases": [{"app": k, "description": v.description} for k, v in sorted(self.cfg.allowed_apps.items())],
            "installed_count": len(apps),
            "installed_examples": [a.name for a in apps[:limit]],
            "note": "Any installed program can be opened by name; these are only examples.",
        }

    # -- starting ------------------------------------------------------------
    def resolve(self, query: str) -> tuple[list[str], str]:
        """(command, program name). Asks instead of guessing when the match is unclear."""
        from .windows import ComputerError

        key = query.strip().lower()
        alias = self.cfg.allowed_apps.get(key)
        if alias is not None:
            return list(alias.command), alias.description or key
        matches = rank(query, self.apps())
        if not matches:
            matches = rank(query, self.apps(force=True))  # installed since the last read
        if not matches:
            extra = f" ({self.last_error})" if self.last_error else ""
            near = ", ".join(a.name for _, a in self._nearest(query, 3))
            hint = f" Närmast: {near}." if near else ""
            raise ComputerError(f"Hittade inget installerat program som heter '{query}'.{extra}{hint}")
        best_score, best_tier, best = matches[0]
        rivals = [m for m in matches[1:] if _fold(m[2].name) != _fold(best.name)]
        # Only an exact match (tier 0) is a clear winner on its own. Otherwise two
        # names that are equally good are a question, not a guess.
        if rivals and best_tier >= 1:
            rival_score, rival_tier, rival = rivals[0]
            if rival_tier == best_tier and best_score - rival_score < FUZZY_MARGIN:
                more = ", ".join(a.name for _, _, a in rivals[1:MAX_CHOICES])
                extra = f" (även: {more})" if more else ""
                raise ComputerError(f"Menade du {best.name} eller {rival.name}?{extra}")
        return ["explorer.exe", f"shell:AppsFolder\\{best.app_id}"], best.name

    def _nearest(self, query: str, limit: int) -> list[tuple[float, App]]:
        scored = sorted(((similarity(query, a.name), a) for a in self.apps()), key=lambda s: -s[0])
        return scored[:limit]

    # -- is it already open? -------------------------------------------------
    def _matching_window(self, name: str):
        """A visible window that belongs to this program, or None."""
        folded = _fold(name)
        squeezed = _squeeze(name)
        best = None
        for w in self._windows():
            title = _fold(w.title)
            if squeezed and _squeeze(w.title).find(squeezed) >= 0:
                return w
            if folded and folded in title:
                best = best or w
        return best

    def open(self, query: str, *, dry_run: bool) -> dict[str, Any]:
        """Start the program, or bring it to the front when it is already open.

        The result says what really happened, so JARVIS never claims to have
        started something it only focused (or did not manage to start at all).
        """
        from .windows import ComputerError

        command, name = self.resolve(query)
        if dry_run:
            return {"app": name, "would_run": command}
        if not self.is_windows():
            raise ComputerError("Att starta program fungerar bara på Windows.")

        existing = self._matching_window(name)
        if existing is not None:
            focused = self._focus(existing.handle)
            log.info("%s was already running; brought it to the front", name)
            return {"app": name, "already_running": True, "focused": bool(focused),
                    "window": existing.title, "verified": bool(focused)}

        self._launch(command)
        window = self._wait_for_window(name)
        log.info("Started %s (window seen: %s)", name, window is not None)
        return {"app": name, "already_running": False, "started": True,
                "window": window.title if window else None, "verified": window is not None}

    def _wait_for_window(self, name: str):
        deadline = self.clock() + WINDOW_WAIT_S
        while self.clock() < deadline:
            window = self._matching_window(name)
            if window is not None:
                return window
            self._sleep(0.4)
        return None


def _launch_detached(command: list[str]) -> None:  # pragma: no cover - starts a real process
    flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    subprocess.Popen(  # noqa: S603 - fixed program plus an AppID that came from Windows itself
        command, shell=False, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL, creationflags=flags, close_fds=True,
    )
