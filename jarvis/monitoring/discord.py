"""Discord phone notifications: OUTGOING ONLY, through one webhook.

* Nothing here receives or reads anything from Discord; there is no bot and no
  gateway connection. A webhook can only post.
* The model has no tool that sends: only the Notifier calls ``send`` with
  notifications built from fixed templates. The webhook URL (the recipient)
  comes only from ``DISCORD_WEBHOOK_URL`` in .env and is validated at start.
* The URL is a secret: it is registered with the log redactor, never logged and
  never shown in the UI (only "Discord: på/av").
* What leaves the PC is minimal: the type and sender/domain ("Viktigt mejl från
  Skatteverket (skatteverket.se)"), plus one sentence with phone_detail =
  "summary". Never mail text or file paths. One-time codes are already masked
  by the Notifier. Mentions are neutralised and allowed_mentions is empty.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections import deque
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

from ..config import DiscordConfig
from .notifier import Notification, NotificationChannel, mask_codes

log = logging.getLogger(__name__)

WEBHOOK_RE = re.compile(r"^https://(?:discord\.com|discordapp\.com)/api/webhooks/\d{5,30}/[A-Za-z0-9_\-]{20,200}$")
COLOR_URGENT = 0xE53935  # red
COLOR_HIGH = 0x3B82F6  # blue
_ZWSP = "​"
_MENTION = re.compile(r"@(everyone|here)", re.I)
_TAG = re.compile(r"<(@[!&]?|#)")


class WebhookError(ValueError):
    pass


def validate_webhook_url(url: str | None) -> str:
    """The webhook URL, or WebhookError with a clear message (never containing the URL)."""
    if not url:
        raise WebhookError("DISCORD_WEBHOOK_URL is not set in .env")
    url = url.strip()
    if not WEBHOOK_RE.match(url):
        raise WebhookError(
            "DISCORD_WEBHOOK_URL is not a Discord webhook. It must look like "
            "https://discord.com/api/webhooks/<id>/<token> (or discordapp.com)."
        )
    return url


def neutralize(text: str) -> str:
    """No pings: @everyone/@here and <@user>, <@&role>, <#channel> are broken with a zero-width space."""
    text = _MENTION.sub(lambda m: "@" + _ZWSP + m.group(1), text)
    return _TAG.sub(lambda m: "<" + _ZWSP + m.group(1), text)


def phone_text(n: Notification, detail_level: str) -> tuple[str, str | None]:
    """(title, description) for the phone, from the notification's fixed-template fields only."""
    title = n.title
    if n.kind == "summary":
        # Only the counts ("Morgonsammanfattning: 2 viktiga mejl, 5 nya filer ..."), never the lines.
        return neutralize(mask_codes(title) or "")[:240], None
    description = None
    if detail_level == "summary" and n.detail and n.source == "mail":
        # One sentence (the model's one-line summary or the subject), never the mail text.
        description = n.detail.split("\n", 1)[0][:200]
    # File details hold folder paths: only the file name in the title ever leaves the PC.
    title = neutralize(mask_codes(title) or "")[:240]
    return title, neutralize(mask_codes(description) or "") or None


def build_payload(n: Notification, detail_level: str) -> dict[str, Any]:
    title, description = phone_text(n, detail_level)
    urgent = n.priority == "urgent"
    embed: dict[str, Any] = {"title": ("🚨 AKUT · " if urgent else "") + title, "color": COLOR_URGENT if urgent else COLOR_HIGH}
    if description:
        embed["description"] = description
    payload: dict[str, Any] = {
        "username": "JARVIS",
        "embeds": [embed],
        "allowed_mentions": {"parse": []},  # never ping anyone, whatever the text says
    }
    if urgent:
        payload["content"] = "🚨 AKUT"
    return payload


class DiscordChannel(NotificationChannel):
    """Interrupting channel for high and urgent notifications, posted to a Discord webhook."""

    name = "discord"
    interrupting = True
    min_priority = "high"

    def __init__(
        self,
        cfg: DiscordConfig,
        webhook_url: str | None,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.cfg = cfg
        self.clock = clock
        self.sleep = sleep
        self.paused = False
        self.error: str | None = None
        self.last_sent: float | None = None
        self._sent: deque[float] = deque()
        self._url: str | None = None
        try:
            self._url = validate_webhook_url(webhook_url)
        except WebhookError as exc:
            self.error = str(exc)
            if webhook_url:
                log.error("Discord notifications are off: %s", exc)
        self._client = httpx.AsyncClient(transport=transport, timeout=cfg.timeout_s)

    @property
    def ready(self) -> bool:
        return self._url is not None

    def accepts(self, n: Notification) -> bool:
        if not self.ready or self.paused:
            return False
        if n.kind == "summary" and n.source == "summary" and n.reason == "hourly limit reached":
            return True  # the morning summary still reaches the phone
        return super().accepts(n)

    def _rate_ok(self) -> bool:
        now = self.clock()
        while self._sent and now - self._sent[0] > 3600:
            self._sent.popleft()
        return len(self._sent) < self.cfg.max_per_hour

    async def send(self, n: Notification) -> bool:
        if not self._rate_ok():
            log.info("Discord: hourly limit (%d) reached; notification kept in JARVIS only", self.cfg.max_per_hour)
            return False
        return await self._post(build_payload(n, self.cfg.phone_detail))

    async def send_test(self) -> bool:
        """A fixed test message (from the UI after confirmation, or `python -m jarvis discord-test`)."""
        if not self.ready:
            return False
        payload = {
            "username": "JARVIS",
            "embeds": [{"title": "Testnotis från JARVIS", "description": "Om du ser detta i mobilen fungerar Discord-notiserna.", "color": COLOR_HIGH}],
            "allowed_mentions": {"parse": []},
        }
        return await self._post(payload)

    async def _post(self, payload: dict[str, Any]) -> bool:
        """POST with up to max_attempts tries; honours 429 retry_after. Never raises."""
        assert self._url is not None
        for attempt in range(1, self.cfg.max_attempts + 1):
            wait: float | None = None
            try:
                r = await self._client.post(self._url, json=payload)
            except httpx.HTTPError as exc:
                log.warning("Discord send failed (%s), attempt %d", type(exc).__name__, attempt)
                wait = float(2 ** (attempt - 1))
            else:
                if r.status_code in (200, 204):
                    self._sent.append(self.clock())
                    self.last_sent = self.clock()
                    self.error = None
                    return True
                if r.status_code == 429:
                    wait = _retry_after(r)
                    log.warning("Discord rate limited; retrying in %.1f s", wait)
                elif r.status_code >= 500:
                    wait = float(2 ** (attempt - 1))
                    log.warning("Discord server error %d, attempt %d", r.status_code, attempt)
                else:
                    # 401/403/404: the webhook was deleted or the URL is wrong. Do not retry.
                    self.error = f"Discord refused the message (HTTP {r.status_code}). Check DISCORD_WEBHOOK_URL."
                    log.error("Discord refused the message (HTTP %d)", r.status_code)
                    return False
            if attempt < self.cfg.max_attempts and wait is not None:
                await self.sleep(min(wait, 30.0))
        self.error = "Discord could not be reached (gave up after retries)."
        return False

    def status(self) -> dict[str, Any]:
        now = self.clock()
        return {
            "configured": self.ready,
            "on": self.ready and not self.paused,
            "paused": self.paused,
            "error": self.error,
            "phone_detail": self.cfg.phone_detail,
            "max_per_hour": self.cfg.max_per_hour,
            "sent_last_hour": sum(1 for t in self._sent if now - t <= 3600),
            "last_sent": self.last_sent,
        }

    def describe(self) -> dict[str, Any]:
        return {**super().describe(), "on": self.ready and not self.paused}

    async def aclose(self) -> None:
        await self._client.aclose()


def _retry_after(r: httpx.Response) -> float:
    try:
        return max(0.0, float(r.json().get("retry_after", 1.0)))
    except (ValueError, AttributeError, TypeError):
        try:
            return max(0.0, float(r.headers.get("retry-after", "1")))
        except ValueError:
            return 1.0
