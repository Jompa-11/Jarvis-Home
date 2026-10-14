"""Provider-neutral mail types. Providers are read-only: they can list messages and nothing else."""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

_CONTROL = re.compile(r"[\x00-\x1f\x7f​-‏‪-‮⁦-⁩]")


def clean_field(text: str | None, limit: int) -> str:
    """One line, no control or bidi characters, bounded length."""
    t = _CONTROL.sub(" ", text or "")
    t = re.sub(r"\s+", " ", t).strip()
    return t[:limit]


@dataclass
class MailMessage:
    id: str  # provider message id (stored only as a hash, see Notifier)
    sender_name: str
    sender_email: str
    subject: str
    snippet: str  # the provider's short preview, never the full body
    received_at: float  # unix time
    labels: list[str] = field(default_factory=list)

    @property
    def sender_display(self) -> str:
        return clean_field(self.sender_name or self.sender_email or "okänd avsändare", 60)

    @property
    def sender_domain(self) -> str:
        return self.sender_email.rsplit("@", 1)[-1].lower() if "@" in self.sender_email else ""


class MailAuthError(Exception):
    """Not signed in, or the sign-in expired/was revoked: run `python -m jarvis mail-login`."""


class MailProvider(ABC):
    name: str

    @abstractmethod
    def signed_in(self) -> bool: ...

    @abstractmethod
    async def list_recent(self, *, after: float, max_results: int) -> list[MailMessage]:
        """Messages received after ``after`` (unix time), newest first. Read-only."""

    async def aclose(self) -> None:  # pragma: no cover - default no-op
        return None
