"""Background scheduler (asyncio) implementing the ``Scheduler`` extension point.

Runs periodic jobs (e.g. "check mail every 15 minutes"), daily jobs (the morning
summary) and one-off reminders. A failing job is logged and retried at its next
time; it never stops the scheduler. ``stop()`` cancels everything cleanly, which
is what happens on Ctrl+C (the server's shutdown calls JarvisCore.stop()).
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from typing import Any

from ..extensions.interfaces import Scheduler

log = logging.getLogger(__name__)

Job = Callable[[], Awaitable[Any]]


@dataclass
class JobInfo:
    name: str
    kind: str  # "every" | "daily" | "once"
    detail: str
    next_run: datetime | None = None
    last_run: datetime | None = None
    last_error: str | None = None
    runs: int = 0
    task: asyncio.Task | None = field(default=None, repr=False)

    def public(self) -> dict[str, Any]:
        return {
            "name": self.name, "kind": self.kind, "detail": self.detail,
            "next_run": self.next_run.isoformat(timespec="minutes") if self.next_run else None,
            "last_run": self.last_run.isoformat(timespec="minutes") if self.last_run else None,
            "last_error": self.last_error, "runs": self.runs,
        }


class AsyncScheduler(Scheduler):
    def __init__(self, *, clock: Callable[[], datetime] = datetime.now, on_reminder: Callable[[str, str], Awaitable[Any]] | None = None) -> None:
        self.clock = clock
        self.on_reminder = on_reminder
        self.jobs: dict[str, JobInfo] = {}
        self.running = False

    # -- lifecycle -----------------------------------------------------------
    def start(self) -> None:
        self.running = True

    async def stop(self) -> None:
        self.running = False
        tasks = [j.task for j in self.jobs.values() if j.task and not j.task.done()]
        for t in tasks:
            t.cancel()
        for t in tasks:
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
        log.info("Scheduler stopped (%d jobs)", len(tasks))

    def status(self) -> list[dict[str, Any]]:
        return [j.public() for j in self.jobs.values()]

    # -- jobs ------------------------------------------------------------------
    async def _sleep_until(self, when: datetime) -> None:
        delay = (when - self.clock()).total_seconds()
        if delay > 0:
            await asyncio.sleep(delay)

    async def _run(self, info: JobInfo, func: Job) -> None:
        info.last_run = self.clock()
        info.runs += 1
        try:
            await func()
            info.last_error = None
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            info.last_error = f"{type(exc).__name__}: {exc}"[:200]
            log.exception("Scheduled job %s failed", info.name)

    def every(self, name: str, seconds: float, func: Job, *, run_now: bool = False) -> JobInfo:
        if seconds <= 0:
            raise ValueError("interval must be positive")
        self.cancel_job(name)
        info = JobInfo(name, "every", f"every {seconds:g} s")

        async def loop() -> None:
            if not run_now:
                info.next_run = self.clock() + timedelta(seconds=seconds)
                await asyncio.sleep(seconds)
            while True:
                await self._run(info, func)
                info.next_run = self.clock() + timedelta(seconds=seconds)
                await asyncio.sleep(seconds)

        info.task = asyncio.create_task(loop(), name=f"jarvis-job-{name}")
        self.jobs[name] = info
        return info

    def daily(self, name: str, at: time, func: Job) -> JobInfo:
        self.cancel_job(name)
        info = JobInfo(name, "daily", f"daily at {at.strftime('%H:%M')}")

        async def loop() -> None:
            while True:
                info.next_run = next_daily(self.clock(), at)
                await self._sleep_until(info.next_run)
                await self._run(info, func)
                await asyncio.sleep(1)  # never run twice in the same minute

        info.task = asyncio.create_task(loop(), name=f"jarvis-job-{name}")
        self.jobs[name] = info
        return info

    def cancel_job(self, name: str) -> bool:
        info = self.jobs.pop(name, None)
        if info and info.task and not info.task.done():
            info.task.cancel()
            return True
        return False

    # -- Scheduler interface (reminders) ----------------------------------------
    async def schedule(self, profile_id: str, when: datetime, text: str) -> str:
        reminder_id = "reminder-" + uuid.uuid4().hex[:10]
        info = JobInfo(reminder_id, "once", f"reminder for {profile_id}", next_run=when)

        async def once() -> None:
            await self._sleep_until(when)
            if self.on_reminder:
                await self._run(info, lambda: self.on_reminder(profile_id, text))
            self.jobs.pop(reminder_id, None)

        info.task = asyncio.create_task(once(), name=f"jarvis-{reminder_id}")
        self.jobs[reminder_id] = info
        return reminder_id

    async def cancel(self, reminder_id: str) -> None:
        self.cancel_job(reminder_id)


def next_daily(now: datetime, at: time) -> datetime:
    """The next time-of-day ``at`` strictly after ``now``."""
    candidate = now.replace(hour=at.hour, minute=at.minute, second=0, microsecond=0)
    return candidate if candidate > now else candidate + timedelta(days=1)
