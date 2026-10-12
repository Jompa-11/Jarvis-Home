"""Conversation context.

The history is append-only, which keeps the cloud prompt cache warm and keeps
Claude's thinking blocks valid between requests. When the history grows past
the retention limit, the oldest complete exchanges are dropped and the stored
provider-specific content of the remaining messages is cleared once (so the
replayed history no longer carries reasoning that referred to dropped turns).
"""

from __future__ import annotations

import time
import uuid

from ..providers.base import ChatMessage


class Conversation:
    def __init__(self, profile_id: str, max_messages: int) -> None:
        self.id = uuid.uuid4().hex[:12]
        self.profile_id = profile_id
        self.created_at = time.time()
        self.max_messages = max_messages
        self._messages: list[ChatMessage] = []

    @property
    def messages(self) -> list[ChatMessage]:
        return list(self._messages)

    def append(self, msg: ChatMessage) -> None:
        self._messages.append(msg)

    def extend(self, msgs: list[ChatMessage]) -> None:
        self._messages.extend(msgs)

    def trim(self) -> bool:
        """Drop oldest exchanges so a new turn fits. Returns True if trimmed."""
        if len(self._messages) <= self.max_messages:
            return False
        # Cut only at a plain user message so tool calls and results stay paired.
        excess = len(self._messages) - self.max_messages
        cut = None
        for i in range(excess, len(self._messages)):
            m = self._messages[i]
            if m.role == "user" and not m.tool_call_id:
                cut = i
                break
        if cut is None or cut == 0:
            return False
        self._messages = self._messages[cut:]
        for m in self._messages:
            m.raw.clear()
        return True

    def transcript(self) -> list[dict]:
        """User-visible transcript (no tool plumbing)."""
        out = []
        for m in self._messages:
            if m.role == "user":
                out.append({"role": "user", "text": m.content})
            elif m.role == "assistant" and m.content:
                out.append({"role": "assistant", "text": m.content})
        return out
