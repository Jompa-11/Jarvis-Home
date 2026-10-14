"""Game mode: keep the GPU free while the owner plays.

Game mode is on while a listed game process runs (e.g. cs2.exe), or while the
GPU has been busier than the threshold for a while (read with nvidia-smi).
While it is on, background work makes no Ollama calls: mail that needs the
local model waits and is classified when the game closes. Rule matches need no
model and are still notified at once. When game mode turns on, the local model
is unloaded from the GPU (keep_alive 0).

Everything here is local and free: the process list and nvidia-smi only.
"""

from __future__ import annotations

import asyncio
import csv
import io
import logging
import shutil
import subprocess
import sys
import time
from collections.abc import Awaitable, Callable
from typing import Any

from ..config import GamingConfig

log = logging.getLogger(__name__)

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def running_process_names() -> set[str]:
    """Lower-case names of running processes (tasklist on Windows, ps elsewhere)."""
    try:
        if sys.platform == "win32":
            out = subprocess.run(["tasklist", "/FO", "CSV", "/NH"], capture_output=True, text=True, timeout=10, creationflags=_NO_WINDOW).stdout
            return {row[0].lower() for row in csv.reader(io.StringIO(out)) if row}
        out = subprocess.run(["ps", "-A", "-o", "comm="], capture_output=True, text=True, timeout=10).stdout
        return {line.strip().rsplit("/", 1)[-1].lower() for line in out.splitlines() if line.strip()}
    except (OSError, subprocess.SubprocessError):
        return set()


def gpu_utilization() -> int | None:
    """Highest GPU utilization in percent from nvidia-smi, or None if unavailable."""
    exe = shutil.which("nvidia-smi")
    if not exe:
        return None
    try:
        out = subprocess.run(
            [exe, "--query-gpu=utilization.gpu", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=10, creationflags=_NO_WINDOW,
        ).stdout
        values = [int(v.strip()) for v in out.splitlines() if v.strip().isdigit()]
        return max(values) if values else None
    except (OSError, subprocess.SubprocessError, ValueError):
        return None


class GamingDetector:
    """Decides whether game mode is on. The process list and GPU reader are injectable for tests."""

    def __init__(
        self,
        cfg: GamingConfig,
        *,
        processes: Callable[[], set[str]] = running_process_names,
        gpu: Callable[[], int | None] = gpu_utilization,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.cfg = cfg
        self.processes = processes
        self.gpu = gpu
        self.clock = clock
        self._gpu_high_since: float | None = None
        self.last_gpu: int | None = None

    def _game_running(self) -> str | None:
        names = {n.lower() for n in self.processes()}
        return next((p for p in self.cfg.process_names if p.lower() in names), None)

    def check(self) -> str | None:
        """Reason game mode is on ("cs2.exe", "GPU 97 % i 2 min"), or None. Call regularly."""
        if not self.cfg.enabled:
            return None
        game = self._game_running()
        util = self.gpu() if self.cfg.gpu_threshold_percent < 100 else None
        self.last_gpu = util
        now = self.clock()
        if util is not None and util > self.cfg.gpu_threshold_percent:
            if self._gpu_high_since is None:
                self._gpu_high_since = now
        else:
            self._gpu_high_since = None
        if game:
            return game
        if self._gpu_high_since is not None and now - self._gpu_high_since >= self.cfg.gpu_high_minutes * 60:
            return f"GPU {util} % i {self.cfg.gpu_high_minutes:g} min"
        return None

    def check_now(self) -> str | None:
        """One-off check without waiting (for toolcheck): a listed game, or the GPU busy right now."""
        if not self.cfg.enabled:
            return None
        game = self._game_running()
        if game:
            return game
        util = self.gpu()
        if util is not None and util > self.cfg.gpu_threshold_percent:
            return f"GPU {util} %"
        return None


class GamingMode:
    """Game mode state plus what happens when it turns on and off."""

    def __init__(
        self,
        detector: GamingDetector,
        *,
        on_start: Callable[[], Awaitable[Any]] | None = None,
        on_end: Callable[[], Awaitable[Any]] | None = None,
        on_change: Callable[[], None] | None = None,
    ) -> None:
        self.detector = detector
        self.on_start = on_start
        self.on_end = on_end
        self.on_change = on_change
        self.reason: str | None = None
        self.since: float | None = None

    @property
    def active(self) -> bool:
        return self.reason is not None

    async def poll(self) -> bool:
        reason = await asyncio.to_thread(self.detector.check)
        was = self.active
        self.reason = reason
        if reason and not was:
            self.since = time.time()
            log.info("Game mode on (%s): background model calls paused", reason)
            if self.on_start:
                await self.on_start()
        elif not reason and was:
            self.since = None
            log.info("Game mode off")
            if self.on_end:
                await self.on_end()
        if was != self.active and self.on_change:
            self.on_change()
        return self.active

    def status(self) -> dict[str, Any]:
        return {"enabled": self.detector.cfg.enabled, "active": self.active, "reason": self.reason, "since": self.since}
