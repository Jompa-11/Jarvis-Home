"""MonitorService: the scheduler, the notifier, the watched sources and the summary.

Sources (mail, folders) register themselves here in later steps; until then
they are listed as "not configured", so "what are you watching?" is answered
truthfully.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..config import MonitoringConfig
from ..rules import RulesConfig
from .notifier import Notification, NotificationChannel, Notifier, SeenStore
from .scheduler import AsyncScheduler

if TYPE_CHECKING:
    from ..core.tools import ToolRegistry

log = logging.getLogger(__name__)


class MonitorService:
    def __init__(
        self,
        cfg: MonitoringConfig,
        get_rules: Callable[[], RulesConfig],
        *,
        channels: list[NotificationChannel] | None = None,
        state_path: Path | None = None,
        clock: Callable[[], datetime] = datetime.now,
        on_change: Callable[[], None] | None = None,
    ) -> None:
        self.cfg = cfg
        self.get_rules = get_rules
        self.clock = clock
        self.on_change = on_change
        self.notifier = Notifier(
            lambda: get_rules().notifications,
            channels=channels,
            seen=SeenStore(state_path, cfg.retention_days),
            clock=clock,
            recent_limit=cfg.recent_limit,
            on_change=lambda n: self._changed(),
        )
        self.scheduler = AsyncScheduler(clock=clock, on_reminder=self._reminder)
        self.sources: dict[str, dict[str, Any]] = {
            "mail": {"state": "not_configured", "detail": "Gmail is added in step 3."},
            "folders": {"state": "not_configured", "detail": "Folder watching is added in step 4."},
        }
        self.last_summary: dict[str, Any] | None = None

    def _changed(self) -> None:
        if self.on_change:
            try:
                self.on_change()
            except Exception:
                log.exception("monitor state listener failed")

    # -- lifecycle -----------------------------------------------------------
    async def start(self) -> None:
        self.scheduler.start()
        self.scheduler.daily("morning_summary", self.get_rules().notifications.morning_time, self.morning_summary)
        self.scheduler.every("prune_state", 6 * 3600, self._prune)
        log.info("Monitoring started (%s)", "paused" if self.paused else "active")

    async def stop(self) -> None:
        await self.scheduler.stop()

    async def _prune(self) -> None:
        self.notifier.seen.prune()

    # -- controls ------------------------------------------------------------
    @property
    def paused(self) -> bool:
        return self.notifier.paused

    def set_paused(self, paused: bool) -> None:
        self.notifier.paused = paused
        self._changed()

    def mute(self, minutes: float) -> None:
        self.notifier.mute(minutes)
        self._changed()

    def set_source(self, name: str, state: str, detail: str) -> None:
        self.sources[name] = {"state": state, "detail": detail}
        self._changed()

    # -- events ----------------------------------------------------------------
    async def _reminder(self, profile_id: str, text: str) -> None:
        await self.notifier.notify(source="reminder", title=f"Påminnelse: {text}", priority="high", kind="reminder")

    async def morning_summary(self) -> Notification | None:
        """Collect what waited (quiet hours, normal and low items) into one summary."""
        items = self.notifier.take_summary()
        now = self.clock()
        self.last_summary = {"created_at": now.timestamp(), "items": [i.public() for i in items]}
        if not items:
            title, detail = "Morgonsammanfattning: inget nytt sedan sist.", None
        else:
            important = sum(1 for i in items if i.priority in ("high", "urgent"))
            title = f"Morgonsammanfattning: {len(items)} {'sak' if len(items) == 1 else 'saker'}" + (f", varav {important} viktiga" if important else "") + "."
            detail = "; ".join(i.title for i in items[:8])
        # Shown in the UI until acknowledged (step 5 adds reading it aloud).
        return await self.notifier.notify(source="summary", title=title, detail=detail, priority="normal", kind="summary", dedupe_key=f"summary:{now:%Y-%m-%d %H:%M}", summarize=False)

    # -- state ---------------------------------------------------------------
    def status(self) -> dict[str, Any]:
        return {
            **self.notifier.status(),
            "sources": self.sources,
            "jobs": self.scheduler.status(),
            "morning_summary_time": self.get_rules().notifications.morning_summary_time,
            "last_summary_at": self.last_summary["created_at"] if self.last_summary else None,
        }

    def snapshot(self) -> dict[str, Any]:
        return {**self.status(), "recent": self.notifier.list_recent(20)}


def register_monitor_tools(registry: "ToolRegistry", monitor: MonitorService) -> None:
    from ..core.tools import Risk, ToolResult, ToolSpec, schema

    async def monitor_status(args, ctx) -> ToolResult:
        now = monitor.clock()
        start = now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        today = [
            {"time": datetime.fromtimestamp(n["created_at"]).strftime("%H:%M"), "priority": n["priority"], "title": n["title"], "delivery": n["delivery"], "reason": n["reason"]}
            for n in monitor.notifier.list_recent(100)
            if n["created_at"] >= start
        ]
        st = monitor.status()
        return ToolResult(
            ok=True,
            sensitivity="personal",
            data={
                "monitoring": "paused" if st["paused"] else "active",
                "muted": st["muted"],
                "quiet_hours": st["quiet_hours"],
                "quiet_now": st["quiet_now"],
                "watching": st["sources"],
                "channels": [c["name"] for c in st["channels"]],
                "today": today,
                "waiting_for_morning_summary": st["waiting_for_summary"],
                "morning_summary_time": st["morning_summary_time"],
            },
        )

    registry.register(
        ToolSpec(
            "monitor_status",
            "What JARVIS is monitoring and what has happened today: watched sources (mail, folders), whether "
            "monitoring is active or paused, quiet hours, and today's notifications. Use for questions like "
            "'what has happened today?' / 'vad har hänt idag?' and 'what are you watching?' / 'vad bevakar du?'.",
            schema({}),
            monitor_status,
            Risk.READ,
            "system",
            output_sensitivity="personal",
        )
    )
