"""Routes each request to a provider chain and applies privacy and budget rules.

Routing is per *task category* (``conversation``, ``smart_home``, ``computer``)
and the category is chosen locally with keyword rules, so no model call is
spent on routing.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import re
import time
from dataclasses import dataclass, field

from ..config import SENSITIVITY_ORDER, AIConfig, PrivacyConfig
from ..core.tools import ToolSpec
from .base import AIProvider, ChatMessage, ProviderError, ProviderHealth, ProviderResponse
from .usage import UsageTracker

log = logging.getLogger(__name__)

WITHHELD = "[Withheld: this tool output is classified '{s}' and is not shared with cloud models under the current privacy settings.]"

_SMART_HOME = re.compile(
    r"\b(sonos|speakers?|högtalar\w*|music|musik\w*|volume|volym\w*|play\w*|spela\w*|paus\w*|resume|"
    r"favou?rit\w*|announc\w*|meddela\w*|lights?|lamp\w*|ljus\w*|home assistant)\b",
    re.I,
)
_COMPUTER = re.compile(r"\b(open|launch|start|öppna|starta|windows?|fönster|app\w*|program\w*)\b", re.I)


def categorize(text: str) -> str:
    if _SMART_HOME.search(text):
        return "smart_home"
    if _COMPUTER.search(text):
        return "computer"
    return "conversation"


@dataclass
class RouteAttempt:
    provider: str
    model: str | None
    outcome: str


@dataclass
class RouteInfo:
    category: str
    provider: str = ""
    model: str = ""
    kind: str = ""
    is_fallback: bool = False
    attempts: list[RouteAttempt] = field(default_factory=list)
    cost_usd: float | None = None


class NoProviderAvailable(Exception):
    def __init__(self, info: RouteInfo) -> None:
        reasons = "; ".join(f"{a.provider}: {a.outcome}" for a in info.attempts) or "no providers configured"
        super().__init__(f"No AI provider could handle the request ({reasons}).")
        self.info = info


class ProviderRouter:
    HEALTH_TTL_S = 30.0

    def __init__(
        self,
        providers: dict[str, AIProvider],
        ai_cfg: AIConfig,
        privacy: PrivacyConfig,
        usage: UsageTracker,
    ) -> None:
        self.providers = providers
        self._ai = ai_cfg
        self._privacy = privacy
        self.usage = usage
        self._health: dict[str, tuple[float, ProviderHealth]] = {}

    def set_privacy(self, privacy: PrivacyConfig) -> None:
        self._privacy = privacy

    async def health(self, name: str, *, force: bool = False) -> ProviderHealth:
        cached = self._health.get(name)
        if cached and not force and time.monotonic() - cached[0] < self.HEALTH_TTL_S:
            return cached[1]
        provider = self.providers[name]
        try:
            h = await asyncio.wait_for(provider.health(), timeout=5.0)
        except Exception as exc:  # health checks must never crash the core
            h = ProviderHealth(False, f"Health check failed: {type(exc).__name__}")
        self._health[name] = (time.monotonic(), h)
        return h

    def invalidate_health(self, name: str) -> None:
        self._health.pop(name, None)

    async def status(self) -> list[dict]:
        out = []
        for name, p in self.providers.items():
            h = await self.health(name)
            out.append({"name": name, "kind": p.kind, "available": h.available, "detail": h.detail})
        return out

    def _skip_reason(self, provider: AIProvider) -> str | None:
        if provider.kind == "dev" and not self._ai.allow_dev_fallback:
            return "dev fallback disabled"
        if provider.kind == "cloud" and self._privacy.local_only:
            return "skipped (local-only privacy mode)"
        if provider.kind == "cloud" and self.usage.budget_exceeded():
            return "skipped (monthly budget reached)"
        return None

    def _filter_for_cloud(self, messages: list[ChatMessage]) -> list[ChatMessage]:
        limit = SENSITIVITY_ORDER[self._privacy.cloud_max_sensitivity]
        out = []
        for m in messages:
            if SENSITIVITY_ORDER.get(m.sensitivity, 2) > limit:
                withheld = WITHHELD.format(s=m.sensitivity)
                if m.role == "assistant":
                    # Keep tool calls (needed to pair results); drop text and raw content.
                    m = dataclasses.replace(m, content=withheld if not m.tool_calls else "", raw={})
                else:
                    m = dataclasses.replace(m, content=withheld)
            out.append(m)
        return out

    async def complete(
        self,
        *,
        category: str,
        system: str,
        messages: list[ChatMessage],
        tools: list[ToolSpec],
        sticky_provider: str | None = None,
    ) -> tuple[ProviderResponse, RouteInfo]:
        chain = self._ai.routes.get(category) or self._ai.routes["conversation"]
        info = RouteInfo(category=category)
        first_choice = True
        if sticky_provider:
            # Later rounds of a turn stay on the provider that answered the first
            # round, instead of retrying providers that already failed.
            idx = next((i for i, r in enumerate(chain) if r.provider == sticky_provider), None)
            if idx:
                chain = chain[idx:]
                first_choice = False
        for route in chain:
            provider = self.providers.get(route.provider)
            if provider is None:
                info.attempts.append(RouteAttempt(route.provider, route.model, "not installed"))
                continue
            skip = self._skip_reason(provider)
            if skip:
                info.attempts.append(RouteAttempt(route.provider, route.model, skip))
                first_choice = False
                continue
            health = await self.health(route.provider)
            if not health.available:
                info.attempts.append(RouteAttempt(route.provider, route.model, health.detail))
                first_choice = False
                continue

            to_send = self._filter_for_cloud(messages) if provider.kind == "cloud" else messages
            try:
                resp = await self._call_with_retry(provider, system, to_send, tools, route.model, route.effort)
            except ProviderError as exc:
                log.warning("Provider %s failed: %s", route.provider, exc)
                info.attempts.append(RouteAttempt(route.provider, route.model, str(exc)))
                self.invalidate_health(route.provider)
                first_choice = False
                if not exc.fallback_ok:
                    raise NoProviderAvailable(info) from exc
                continue

            info.provider = provider.name
            info.model = resp.model
            info.kind = provider.kind
            info.is_fallback = (not first_choice) or provider.kind == "dev"
            info.attempts.append(RouteAttempt(route.provider, resp.model, "ok"))
            if provider.kind != "dev":
                info.cost_usd = self.usage.record(provider.name, resp.model, resp.usage)
            return resp, info
        raise NoProviderAvailable(info)

    async def _call_with_retry(self, provider, system, messages, tools, model, effort) -> ProviderResponse:
        # The Anthropic SDK already retries transient errors internally.
        attempts = 1 if provider.name == "anthropic" else 2
        for i in range(attempts):
            try:
                return await asyncio.wait_for(
                    provider.complete(
                        system=system,
                        messages=messages,
                        tools=tools,
                        model=model,
                        effort=effort,
                        timeout_s=self._ai.request_timeout_s,
                    ),
                    timeout=self._ai.request_timeout_s + 5,
                )
            except asyncio.TimeoutError as exc:
                err = ProviderError(f"{provider.name} timed out.", retryable=True)
                if i + 1 >= attempts:
                    raise err from exc
            except ProviderError as exc:
                if not exc.retryable or i + 1 >= attempts:
                    raise
            await asyncio.sleep(0.5 * (i + 1))
        raise ProviderError(f"{provider.name} failed.")  # pragma: no cover
