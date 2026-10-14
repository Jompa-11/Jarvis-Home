"""Keeps the JARVIS server running.

The server runs as a child process (``python -m jarvis --serve-child``), so even a
hard crash of the server cannot take the window and the tray icon with it. If it
stops without being asked to, it is restarted with a growing delay, at most
``max_per_hour`` times per hour; after that the supervisor gives up, logs it and
shows a Windows notification. Stopping on purpose (Avsluta, shutdown) closes the
child's stdin, which the child treats as "stop now", and is never counted as a crash.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import threading
import time
from collections import deque
from collections.abc import Callable, Sequence
from typing import Protocol

from ..config import PROJECT_ROOT

log = logging.getLogger(__name__)


class Proc(Protocol):
    stdin: object | None

    def wait(self, timeout: float | None = None) -> int: ...
    def poll(self) -> int | None: ...
    def terminate(self) -> None: ...
    def kill(self) -> None: ...


class RestartPolicy:
    """At most ``max_per_hour`` restarts in any hour, with growing delays."""

    def __init__(self, max_per_hour: int = 5, backoff_s: Sequence[float] = (2, 5, 15, 30, 60), clock: Callable[[], float] = time.monotonic) -> None:
        self.max_per_hour = max_per_hour
        self.backoff_s = list(backoff_s)
        self.clock = clock
        self.crashes: deque[float] = deque()

    def on_crash(self) -> float | None:
        """Delay before the next restart, or None: give up."""
        now = self.clock()
        while self.crashes and now - self.crashes[0] > 3600:
            self.crashes.popleft()
        if len(self.crashes) >= self.max_per_hour:
            return None
        self.crashes.append(now)
        return self.backoff_s[min(len(self.crashes), len(self.backoff_s)) - 1]


def spawn_server(token: str) -> subprocess.Popen:  # pragma: no cover - starts a real process
    """The server child: same interpreter (pythonw = no console), token in its environment only."""
    env = dict(os.environ)
    env["JARVIS_API_TOKEN"] = token
    return subprocess.Popen(
        [sys.executable, "-m", "jarvis", "--serve-child"],
        cwd=str(PROJECT_ROOT),
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


class ServerSupervisor:
    def __init__(
        self,
        start: Callable[[], Proc],
        policy: RestartPolicy | None = None,
        *,
        on_started: Callable[[], None] | None = None,
        on_gave_up: Callable[[], None] | None = None,
    ) -> None:
        self.start = start
        self.policy = policy or RestartPolicy()
        self.on_started = on_started
        self.on_gave_up = on_gave_up
        self.proc: Proc | None = None
        self.stopping = False
        self.gave_up = False
        self.starts = 0
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None

    def run_in_thread(self) -> "ServerSupervisor":
        self._thread = threading.Thread(target=self.run, name="jarvis-supervisor", daemon=True)
        self._thread.start()
        return self

    def run(self) -> None:
        while not self.stopping:
            self.proc = self.start()
            self.starts += 1
            if self.on_started:
                try:
                    self.on_started()
                except Exception:
                    log.exception("on_started failed")
            code = self.proc.wait()
            if self.stopping:
                break
            delay = self.policy.on_crash()
            if delay is None:
                self.gave_up = True
                log.error("JARVIS server stopped %d times within an hour (last exit code %s); not restarting", self.policy.max_per_hour, code)
                if self.on_gave_up:
                    try:
                        self.on_gave_up()
                    except Exception:
                        log.exception("on_gave_up failed")
                return
            log.warning("JARVIS server stopped unexpectedly (exit code %s); restarting in %.0f s", code, delay)
            self._wake.wait(delay)

    def stop(self, timeout_s: float = 10.0) -> None:
        """Stop on purpose: not a crash, never restarted."""
        self.stopping = True
        self._wake.set()
        proc = self.proc
        if proc is None or proc.poll() is not None:
            return
        try:
            if proc.stdin is not None:
                proc.stdin.close()  # type: ignore[attr-defined]  # the child stops cleanly on EOF
            proc.wait(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            log.warning("Server did not stop in %.0f s; terminating it", timeout_s)
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
        except OSError:
            pass
        if self._thread is not None:
            self._thread.join(timeout=5)


def notify_stopped() -> None:  # pragma: no cover - shows a real Windows notification
    """Windows notification "JARVIS har stannat" (fixed text)."""
    import asyncio

    from ..monitoring.channels import WindowsToastChannel
    from ..monitoring.notifier import Notification

    n = Notification(
        source="system", title="JARVIS har stannat", priority="high", kind="system",
        detail="Servern kraschade 5 gånger på en timme. Se data\\logs\\jarvis.log och starta JARVIS igen.",
        delivery="immediate",
    )
    try:
        asyncio.run(WindowsToastChannel().send(n))
    except Exception:
        log.exception("Could not show the 'JARVIS har stannat' notification")
