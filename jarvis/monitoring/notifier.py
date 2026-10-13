"""Notifier: decides how loudly each event is delivered, and to which channels.

Priorities (lowest to highest): low, normal, high, urgent.

    low     -> the summary only
    normal  -> passive channels (the UI list) + the summary
    high    -> interrupting channels too (Windows notification; later voice/phone),
               unless quiet hours, muted, paused or the hourly limit -> UI + summary
    urgent  -> like high, but may break through quiet hours (rules:
               urgent_overrides_quiet_hours), is not limited per hour and is not
               silenced by "mute". Pausing monitoring still holds it back.

The same event (same ``dedupe_key``) is never delivered twice. Only a hash of
the key and a timestamp are stored on disk (retention configurable); titles and
details stay in memory, so no message text is written to disk.

Channels are pluggable (``NotificationChannel``): the model has no tool that
sends notifications; only code calls ``Notifier.notify`` with fixed templates.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
import uuid
from abc import ABC, abstractmethod
from collections import deque
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from ..rules import NotificationRules

log = logging.getLogger(__name__)

Priority = Literal["low", "normal", "high", "urgent"]
PRIORITY_ORDER: dict[str, int] = {"low": 0, "normal": 1, "high": 2, "urgent": 3}
Delivery = Literal["immediate", "passive", "summary"]


@dataclass
class Notification:
    source: str  # "mail", "files", "reminder", "system", ...
    title: str  # short, fixed-template text, e.g. "Viktigt mejl från Skatteverket"
    priority: Priority
    detail: str | None = None  # optional one line
    kind: str = "info"
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    created_at: float = field(default_factory=time.time)
    delivery: Delivery = "summary"
    reason: str = ""  # why it was delivered this way (shown in the UI)
    channels: list[str] = field(default_factory=list)  # channels that accepted it
    in_summary: bool = False
    acknowledged: bool = False

    def public(self) -> dict[str, Any]:
        return asdict(self)


class NotificationChannel(ABC):
    """A place notifications go. Voice and phone channels plug in later.

    ``interrupting`` channels get someone's attention (pop-up, voice, phone)
    and only receive immediate deliveries; passive ones (the UI list) receive
    everything from ``min_priority`` up.
    """

    name: str
    interrupting: bool = True
    min_priority: Priority = "high"

    def accepts(self, n: Notification) -> bool:
        if PRIORITY_ORDER[n.priority] < PRIORITY_ORDER[self.min_priority]:
            return False
        return n.delivery == "immediate" if self.interrupting else n.delivery in ("immediate", "passive")

    @abstractmethod
    async def send(self, n: Notification) -> bool:
        """Deliver; return True on success. Must never raise for a delivery failure."""

    def describe(self) -> dict[str, Any]:
        return {"name": self.name, "interrupting": self.interrupting, "min_priority": self.min_priority}


def decide(priority: Priority, *, quiet: bool, muted: bool, paused: bool, rate_ok: bool, urgent_overrides_quiet: bool) -> tuple[Delivery, str]:
    """Pure decision: how an event of ``priority`` is delivered right now."""
    if priority == "low":
        return "summary", "low priority"
    if paused:
        return "summary", "monitoring paused"
    if priority == "normal":
        return "passive", "normal priority"
    if priority == "urgent":
        if quiet and not urgent_overrides_quiet:
            return "summary", "quiet hours"
        return "immediate", "urgent"
    # high
    if quiet:
        return "summary", "quiet hours"
    if muted:
        return "passive", "notifications muted"
    if not rate_ok:
        return "passive", "hourly limit reached"
    return "immediate", "high priority"


class SeenStore:
    """Hashes of already-notified event keys with a timestamp. No content."""

    def __init__(self, path: Path | None, retention_days: int) -> None:
        self.path = path
        self.retention_s = retention_days * 86400
        self._seen: dict[str, float] = {}
        if path and path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                self._seen = {k: float(v) for k, v in data.get("seen", {}).items()}
            except (OSError, ValueError):
                log.warning("Notification state file unreadable; starting empty")
        self.prune()

    @staticmethod
    def _h(key: str) -> str:
        return hashlib.sha256(key.encode("utf-8")).hexdigest()[:32]

    def seen(self, key: str) -> bool:
        return self._h(key) in self._seen

    def add(self, key: str, now: float | None = None) -> None:
        self._seen[self._h(key)] = now or time.time()
        self._save()

    def prune(self, now: float | None = None) -> int:
        cutoff = (now or time.time()) - self.retention_s
        old = [k for k, ts in self._seen.items() if ts < cutoff]
        for k in old:
            del self._seen[k]
        if old:
            self._save()
        return len(old)

    def __len__(self) -> int:
        return len(self._seen)

    def _save(self) -> None:
        if not self.path:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps({"seen": self._seen}), encoding="utf-8")
        except OSError:
            log.warning("Could not save notification state")


class Notifier:
    def __init__(
        self,
        get_rules: Callable[[], NotificationRules],
        *,
        channels: list[NotificationChannel] | None = None,
        seen: SeenStore | None = None,
        clock: Callable[[], datetime] = datetime.now,
        recent_limit: int = 100,
        on_change: Callable[[Notification], None] | None = None,
    ) -> None:
        self.get_rules = get_rules
        self.channels = list(channels or [])
        # "is not None": an empty SeenStore is falsy (it has __len__) but must still be used.
        self.seen = seen if seen is not None else SeenStore(None, 14)
        self.clock = clock
        self.recent: deque[Notification] = deque(maxlen=recent_limit)
        self.summary: list[Notification] = []  # waiting for the next summary
        self.paused = False
        self.muted_until: float | None = None
        self.on_change = on_change
        self._interrupt_times: deque[float] = deque()
        self._lock = asyncio.Lock()

    # -- state ---------------------------------------------------------------
    @property
    def muted(self) -> bool:
        return self.muted_until is not None and self.clock().timestamp() < self.muted_until

    def mute(self, minutes: float) -> None:
        self.muted_until = self.clock().timestamp() + minutes * 60 if minutes > 0 else None

    def _rate_ok(self, now: float) -> bool:
        while self._interrupt_times and now - self._interrupt_times[0] > 3600:
            self._interrupt_times.popleft()
        return len(self._interrupt_times) < self.get_rules().max_announcements_per_hour

    def status(self) -> dict[str, Any]:
        now = self.clock()
        rules = self.get_rules()
        start = now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        return {
            "paused": self.paused,
            "muted": self.muted,
            "muted_until": self.muted_until if self.muted else None,
            "quiet_now": rules.is_quiet(now),
            "quiet_hours": f"{rules.quiet_hours_start}-{rules.quiet_hours_end}",
            "channels": [c.describe() for c in self.channels],
            "today": sum(1 for n in self.recent if n.created_at >= start),
            "waiting_for_summary": len(self.summary),
            "unacknowledged": sum(1 for n in self.recent if not n.acknowledged and n.delivery != "summary"),
        }

    def list_recent(self, limit: int = 30) -> list[dict[str, Any]]:
        return [n.public() for n in list(self.recent)[::-1][:limit]]

    def acknowledge(self, notification_id: str | None = None) -> int:
        count = 0
        for n in self.recent:
            if (notification_id is None or n.id == notification_id) and not n.acknowledged:
                n.acknowledged = True
                count += 1
        return count

    # -- delivery ------------------------------------------------------------
    async def notify(self, *, source: str, title: str, priority: Priority, detail: str | None = None, kind: str = "info", dedupe_key: str | None = None, summarize: bool = True) -> Notification | None:
        """Deliver an event. Returns None if this ``dedupe_key`` was already notified.

        ``summarize=False`` keeps it out of the next summary (used for the summary itself).
        """
        if priority not in PRIORITY_ORDER:
            raise ValueError(f"unknown priority {priority!r}")
        async with self._lock:
            if dedupe_key and self.seen.seen(dedupe_key):
                return None
            now = self.clock()
            rules = self.get_rules()
            n = Notification(source=source, title=title[:200], priority=priority, detail=(detail or None) and detail[:300], kind=kind, created_at=now.timestamp())
            n.delivery, n.reason = decide(
                priority,
                quiet=rules.is_quiet(now),
                muted=self.muted and priority != "urgent",
                paused=self.paused,
                rate_ok=priority == "urgent" or self._rate_ok(now.timestamp()),
                urgent_overrides_quiet=rules.urgent_overrides_quiet_hours,
            )
            if n.delivery == "immediate" and priority != "urgent":
                self._interrupt_times.append(now.timestamp())
            if n.delivery != "immediate" and summarize:
                # Anything that did not get the owner's attention right away waits for the summary.
                n.in_summary = True
                self.summary.append(n)
            if dedupe_key:
                self.seen.add(dedupe_key, now.timestamp())
            self.recent.append(n)
        for ch in self.channels:
            if not ch.accepts(n):
                continue
            try:
                if await ch.send(n):
                    n.channels.append(ch.name)
            except Exception:
                log.exception("Notification channel %s failed", ch.name)
        if self.on_change:
            self.on_change(n)
        return n

    def take_summary(self) -> list[Notification]:
        """Hand over everything waiting for the summary and start a new batch."""
        items, self.summary = self.summary, []
        return items
