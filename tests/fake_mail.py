"""A fake, read-only mail provider for tests: no network, no token, no keyring."""

from __future__ import annotations

import time

from jarvis.mail.base import MailAuthError, MailMessage, MailProvider


def mail(id: str, sender: str = "Anna Svensson <anna@example.se>", subject: str = "Hej", snippet: str = "", minutes_ago: float = 5) -> MailMessage:
    name, _, addr = sender.partition("<")
    return MailMessage(
        id=id,
        sender_name=name.strip(),
        sender_email=addr.rstrip(">").strip().lower() or name.strip().lower(),
        subject=subject,
        snippet=snippet,
        received_at=time.time() - minutes_ago * 60,
    )


class FakeMailProvider(MailProvider):
    name = "fake"

    def __init__(self, messages: list[MailMessage] | None = None, *, signed_in: bool = True, fail_auth: bool = False) -> None:
        self.messages = list(messages or [])
        self._signed_in = signed_in
        self.fail_auth = fail_auth
        self.calls: list[dict] = []

    def signed_in(self) -> bool:
        return self._signed_in

    async def list_recent(self, *, after: float, max_results: int) -> list[MailMessage]:
        self.calls.append({"after": after, "max_results": max_results})
        if self.fail_auth:
            raise MailAuthError("The Gmail sign-in has expired or was revoked. Run: python -m jarvis mail-login")
        return [m for m in self.messages if m.received_at > after][:max_results]
