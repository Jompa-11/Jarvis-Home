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


def rank(query: str, apps: list[App]) -> list[App]:
    """Apps matching ``query``, best first: exact name, then prefix, then substring, then words."""
    q = _fold(query)
    if not q:
        return []
    words = [w for w in q.split() if w]
    scored: list[tuple[int, int, App]] = []
    for app in apps:
        name = _fold(app.name)
        if name == q:
            score = 0
        elif name.startswith(q):
            score = 1
        elif q in name:
            score = 2
        elif words and all(w in name for w in words):
            score = 3
        else:
            continue
        scored.append((score, len(name), app))
    scored.sort(key=lambda s: (s[0], s[1], s[2].name))
    return [app for _, _, app in scored]


class AppLauncher:
    """The Start-menu index plus the allowlist aliases from the config."""

    def __init__(
        self,
        cfg: ComputerConfig,
        *,
        list_apps: Callable[[], list[App]] = read_start_apps,
        launch: Callable[[list[str]], None] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.cfg = cfg
        self._list_apps = list_apps
        self._launch = launch or _launch_detached
        self.clock = clock
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
        """(command, what was started). Raises ComputerError when nothing or too much matches."""
        from .windows import ComputerError

        key = query.strip().lower()
        alias = self.cfg.allowed_apps.get(key)
        if alias is not None:
            return list(alias.command), alias.description or key
        matches = rank(query, self.apps())
        if not matches:
            matches = rank(query, self.apps(force=True))  # a program installed since the last read
        if not matches:
            extra = f" ({self.last_error})" if self.last_error else ""
            raise ComputerError(f"Hittade inget installerat program som heter '{query}'.{extra}")
        best = matches[0]
        others = [m for m in matches[1:] if _fold(m.name) != _fold(best.name)]
        # One clear winner (exact or prefix match) wins even when more programs contain the word.
        if others and not (_fold(best.name) == _fold(query) or _fold(best.name).startswith(_fold(query))):
            names = ", ".join(m.name for m in matches[:MAX_CHOICES])
            raise ComputerError(f"Flera program matchar '{query}': {names}. Vilket menar du?")
        return ["explorer.exe", f"shell:AppsFolder\\{best.app_id}"], best.name

    def open(self, query: str, *, dry_run: bool) -> dict[str, Any]:
        command, name = self.resolve(query)
        if dry_run:
            return {"app": name, "would_run": command}
        from .windows import ComputerError

        if sys.platform != "win32":
            raise ComputerError("Att starta program fungerar bara på Windows.")
        self._launch(command)
        log.info("Started program %s", name)
        return {"app": name, "started": True}


def _launch_detached(command: list[str]) -> None:  # pragma: no cover - starts a real process
    flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    subprocess.Popen(  # noqa: S603 - fixed program plus an AppID that came from Windows itself
        command, shell=False, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL, creationflags=flags, close_fds=True,
    )
