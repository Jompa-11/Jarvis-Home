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
from .notifier import Notification, NotificationChannel, Notifier, SeenStore, mask_codes
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
            "folders": {"state": "not_configured", "detail": "Folder watching is off (config [files] enabled)."},
        }
        self.last_summary: dict[str, Any] | None = None
        self.digests: dict[str, Callable[[float | None], Any]] = {}

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

    def add_digest(self, name: str, fn: Callable[[float | None], Any]) -> None:
        """A source that contributes to the summary: fn(since_timestamp) -> its section."""
        self.digests[name] = fn

    async def morning_summary(self, *, on_demand: bool = False) -> Notification | None:
        """Important mail (one sentence each), new files (count + the 5 most important names)
        and what waited (quiet hours, normal and low items), since the previous summary.

        Shown as a notification and as a card in the UI until acknowledged.
        """
        now = self.clock()
        since = self.last_summary["created_at"] if self.last_summary else None
        items = self.notifier.take_summary()
        mail: list[str] = list(self.digests["mail"](since)) if "mail" in self.digests else []
        files: dict[str, Any] = self.digests["files"](since) if "files" in self.digests else {"count": 0, "top": []}
        # New files are counted in their own section; important mail too, when mail is watched.
        waited = [
            i for i in items
            if not ("files" in self.digests and i.source == "files") and not ("mail" in self.digests and i.source == "mail" and i.priority in ("high", "urgent"))
        ]
        label = "Sammanfattning" if on_demand else "Morgonsammanfattning"
        parts = []
        if mail:
            parts.append(f"{len(mail)} viktiga mejl" if len(mail) != 1 else "1 viktigt mejl")
        if files["count"]:
            parts.append(f"{files['count']} {'ny fil' if files['count'] == 1 else 'nya filer'}")
        if waited:
            parts.append(f"{len(waited)} {'sak' if len(waited) == 1 else 'saker'} som väntat")
        title = f"{label}: " + (", ".join(parts) if parts else "inget nytt sedan sist") + "."
        detail_parts = []
        if mail:
            detail_parts.append("Mejl: " + " ".join(mail[:3]))
        if files["top"]:
            detail_parts.append("Filer: " + ", ".join(files["top"]))
        if waited:
            detail_parts.append("Väntat: " + "; ".join(i.title for i in waited[:5]))
        rules = self.get_rules().notifications
        # The scheduled summary may pop up (Windows); on demand the owner is already looking.
        priority = "normal" if on_demand or rules.is_quiet(now) else "high"
        n = await self.notifier.notify(
            source="summary", title=title, detail=" · ".join(detail_parts) or None, priority=priority, kind="summary",
            dedupe_key=None if on_demand else f"summary:{now:%Y-%m-%d %H:%M}", summarize=False,
        )
        self.last_summary = {
            "created_at": now.timestamp(),
            "since": since,
            "on_demand": on_demand,
            "title": n.title if n else title,
            "mail": [mask_codes(m) for m in mail],
            "files": files,
            "waited": [{"time": datetime.fromtimestamp(i.created_at).strftime("%H:%M"), "priority": i.priority, "title": i.title} for i in waited],
            "items": [i.public() for i in items],
            "notification_id": n.id if n else None,
        }
        self._changed()
        return n

    async def summary_now(self) -> dict[str, Any]:
        await self.morning_summary(on_demand=True)
        return self.summary_public() or {}

    def summary_public(self) -> dict[str, Any] | None:
        """The latest summary for the UI card, with whether it has been acknowledged."""
        if not self.last_summary:
            return None
        nid = self.last_summary["notification_id"]
        n = next((x for x in self.notifier.recent if x.id == nid), None)
        out = {k: v for k, v in self.last_summary.items() if k != "items"}
        out["acknowledged"] = True if n is None else n.acknowledged
        return out

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
        return {**self.status(), "recent": self.notifier.list_recent(20), "summary": self.summary_public()}


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
            "personal",
            output_sensitivity="personal",
        )
    )

    async def monitor_summary_now(args, ctx) -> ToolResult:
        s = await monitor.summary_now()
        return ToolResult(
            ok=True,
            sensitivity="personal",
            data={k: s.get(k) for k in ("title", "mail", "files", "waited")},
        )

    registry.register(
        ToolSpec(
            "monitor_summary_now",
            "Make a summary now: important e-mail, new files and what has waited since the last summary. "
            "Use for 'summarize the day' / 'sammanfatta dagen' / 'sammanfatta nu'.",
            schema({}),
            monitor_summary_now,
            Risk.LOW,
            "personal",
            output_sensitivity="personal",
        )
    )
