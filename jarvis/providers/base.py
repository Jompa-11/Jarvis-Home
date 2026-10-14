"""Provider-neutral types for AI model access.

The orchestrator only speaks these types. Each provider translates them to
its own wire format, so a provider can be swapped without touching the core.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Literal

from ..core.tools import ToolSpec

ProviderKind = Literal["cloud", "local", "dev"]


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class ChatMessage:
    """One entry in a conversation, stored append-only.

    ``role`` is ``user``, ``assistant`` or ``tool``. ``raw`` keeps the
    provider's exact assistant content (for example Claude's thinking blocks),
    keyed by provider name, so it can be replayed unchanged to that provider.
    """

    role: Literal["user", "assistant", "tool"]
    content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    tool_call_id: str | None = None
    tool_name: str | None = None
    is_error: bool = False
    sensitivity: str = "normal"
    # True when this content was written by someone outside the household
    # (e-mail, file names, clipboard, window titles). While such a message is
    # still in the conversation, actions need the owner's confirmation.
    untrusted: bool = False
    raw: dict[str, Any] = field(default_factory=dict)
    # Per-turn situational context (time, profile, dry-run) sent with a user
    # message. Kept separate so the visible transcript shows only what was typed.
    context: str = ""

    def model_text(self) -> str:
        if self.role == "user" and self.context:
            return f"<context>\n{self.context}\n</context>\n\n{self.content}"
        return self.content


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0


@dataclass
class ProviderResponse:
    text: str
    tool_calls: list[ToolCall]
    stop_reason: str
    usage: Usage
    provider: str
    model: str
    raw_assistant: Any = None


@dataclass
class ProviderHealth:
    available: bool
    detail: str
    models: list[str] = field(default_factory=list)


class ProviderError(Exception):
    def __init__(self, message: str, *, retryable: bool = False, fallback_ok: bool = True) -> None:
        super().__init__(message)
        self.retryable = retryable
        # False for errors where trying another provider would not help or
        # would be wrong (for example an invalid request we built).
        self.fallback_ok = fallback_ok


class AIProvider(ABC):
    name: str
    kind: ProviderKind

    @abstractmethod
    async def health(self) -> ProviderHealth: ...

    @abstractmethod
    async def complete(
        self,
        *,
        system: str,
        messages: list[ChatMessage],
        tools: list[ToolSpec],
        model: str | None,
        effort: str | None,
        timeout_s: float,
    ) -> ProviderResponse: ...

    async def aclose(self) -> None:  # pragma: no cover - default no-op
        return None
