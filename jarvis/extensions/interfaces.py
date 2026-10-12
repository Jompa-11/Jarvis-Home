"""Extension points for later phases.

These are contracts only. Each future module implements one of these and
registers tools through :class:`jarvis.core.tools.ToolRegistry`, so the core
does not change. Rules that apply to every module:

* Output that contains personal data must set ``ToolSpec.output_sensitivity``
  (``personal`` or ``restricted``) so the router withholds it from cloud models
  unless the user allows it.
* Identity (voice or face) is a *hint*, never authorization. Sensitive actions
  still go through the permission policy and explicit confirmation.
* Raw audio and camera frames are processed locally and not retained.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from typing import Any


# --- Phase 3: camera gestures -------------------------------------------------
@dataclass
class GestureEvent:
    gesture: str  # e.g. "swipe_left", "open_palm", "point"
    confidence: float
    timestamp: float


class GestureRecognizer(ABC):
    """Runs locally on camera frames; emits named gestures only (no images)."""

    @abstractmethod
    async def start(self) -> None: ...

    @abstractmethod
    async def stop(self) -> None: ...

    @abstractmethod
    def events(self): ...  # AsyncIterator[GestureEvent]


# --- Phase 4: household identity --------------------------------------------
@dataclass
class IdentityHint:
    profile_id: str
    method: str  # "voice" | "face"
    confidence: float


class IdentityProvider(ABC):
    """Enrollment requires explicit consent per household member."""

    @abstractmethod
    async def enroll(self, profile_id: str, consent_token: str) -> None: ...

    @abstractmethod
    async def identify(self, sample: Any) -> IdentityHint | None: ...

    @abstractmethod
    async def forget(self, profile_id: str) -> None:
        """Delete all biometric data for a profile."""


# --- Phase 5: calendars ---------------------------------------------------------
@dataclass
class CalendarEvent:
    calendar: str
    title: str
    start: datetime
    end: datetime
    location: str | None = None


class CalendarProvider(ABC):
    @abstractmethod
    async def events(self, profile_id: str, start: datetime, end: datetime) -> list[CalendarEvent]: ...


# --- Phase 8: memory -------------------------------------------------------------
class MemoryStore(ABC):
    """Persistent memory with per-profile scoping and user-visible deletion."""

    @abstractmethod
    async def remember(self, profile_id: str, fact: str, sensitivity: str) -> str: ...

    @abstractmethod
    async def recall(self, profile_id: str, query: str, limit: int = 5) -> list[str]: ...

    @abstractmethod
    async def forget(self, profile_id: str, memory_id: str | None = None) -> None: ...


# --- Phase 9: reminders and planning ----------------------------------------------
class Scheduler(ABC):
    @abstractmethod
    async def schedule(self, profile_id: str, when: datetime, text: str) -> str: ...

    @abstractmethod
    async def cancel(self, reminder_id: str) -> None: ...
