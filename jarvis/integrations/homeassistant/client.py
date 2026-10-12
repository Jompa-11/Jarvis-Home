"""Home Assistant REST API client.

Uses a long-lived access token. Only services on the configured allowlist can
be called; everything else is refused before a request is made.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any

import httpx

from ...config import HomeAssistantConfig
from ...secrets import get_secret

log = logging.getLogger(__name__)


class HAError(Exception):
    """A Home Assistant request failed. ``kind`` is a short machine-readable reason."""

    def __init__(self, message: str, kind: str = "error") -> None:
        super().__init__(message)
        self.kind = kind


@dataclass
class HAStatus:
    state: str  # not_configured | disabled | connected | auth_failed | unreachable | error
    detail: str
    version: str | None = None
    location_name: str | None = None
    checked_at: float = 0.0

    def public(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "detail": self.detail,
            "version": self.version,
            "location_name": self.location_name,
            "checked_at": self.checked_at,
        }


class HomeAssistantClient:
    def __init__(self, cfg: HomeAssistantConfig, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.cfg = cfg
        self._transport = transport
        self._client: httpx.AsyncClient | None = None
        self.status = HAStatus("not_configured", "Home Assistant URL or token not set.")

    @property
    def configured(self) -> bool:
        return bool(self.cfg.enabled and self.cfg.url and get_secret(self.cfg.token_env))

    def _http(self) -> httpx.AsyncClient:
        if not self.cfg.enabled:
            raise HAError("Home Assistant integration is disabled.", "disabled")
        token = get_secret(self.cfg.token_env)
        if not self.cfg.url or not token:
            raise HAError(
                f"Home Assistant is not configured (set homeassistant.url and {self.cfg.token_env}).", "not_configured"
            )
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self.cfg.url,
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                timeout=self.cfg.timeout_s,
                verify=self.cfg.verify_ssl,
                transport=self._transport,
            )
        return self._client

    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        client = self._http()
        try:
            r = await client.request(method, path, **kwargs)
        except httpx.TimeoutException as exc:
            raise HAError("Home Assistant did not respond in time.", "timeout") from exc
        except httpx.HTTPError as exc:
            raise HAError(f"Cannot reach Home Assistant at {self.cfg.url}.", "unreachable") from exc
        if r.status_code == 401:
            raise HAError("Home Assistant rejected the access token (401).", "auth_failed")
        if r.status_code == 404:
            raise HAError(f"Not found: {path}", "not_found")
        if r.status_code >= 400:
            raise HAError(f"Home Assistant returned HTTP {r.status_code} for {path}.", "error")
        if not r.content:
            return None
        ctype = r.headers.get("content-type", "")
        return r.json() if "json" in ctype else r.text

    # -- health ----------------------------------------------------------------
    async def check(self) -> HAStatus:
        now = time.time()
        if not self.cfg.enabled:
            self.status = HAStatus("disabled", "Disabled in configuration.", checked_at=now)
            return self.status
        if not self.configured:
            self.status = HAStatus(
                "not_configured", f"Set homeassistant.url and {self.cfg.token_env} to connect.", checked_at=now
            )
            return self.status
        try:
            ping = await self._request("GET", "/api/")
            if not (isinstance(ping, dict) and ping.get("message")):
                raise HAError("Unexpected response from /api/.")
            conf = await self._request("GET", "/api/config")
            self.status = HAStatus(
                "connected",
                "Connected.",
                version=conf.get("version") if isinstance(conf, dict) else None,
                location_name=conf.get("location_name") if isinstance(conf, dict) else None,
                checked_at=now,
            )
        except HAError as exc:
            state = exc.kind if exc.kind in {"auth_failed", "unreachable", "timeout"} else "error"
            self.status = HAStatus("unreachable" if state == "timeout" else state, str(exc), checked_at=now)
        return self.status

    # -- reads -------------------------------------------------------------------
    async def get_states(self) -> list[dict[str, Any]]:
        data = await self._request("GET", "/api/states")
        return data if isinstance(data, list) else []

    async def get_state(self, entity_id: str) -> dict[str, Any]:
        data = await self._request("GET", f"/api/states/{entity_id}")
        if not isinstance(data, dict):
            raise HAError(f"Unexpected state payload for {entity_id}.")
        return data

    async def render_template(self, template: str) -> str:
        data = await self._request("POST", "/api/template", json={"template": template})
        return data if isinstance(data, str) else str(data)

    # -- actions -----------------------------------------------------------------
    async def call_service(self, domain: str, service: str, data: dict[str, Any]) -> Any:
        name = f"{domain}.{service}"
        if name not in self.cfg.allowed_services:
            raise HAError(f"Service '{name}' is not on the JARVIS allowlist.", "not_allowed")
        log.info("Calling Home Assistant service %s for %s", name, data.get("entity_id"))
        return await self._request("POST", f"/api/services/{domain}/{service}", json=data)

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def reset(self) -> None:
        """Drop the HTTP client so a changed token or URL is picked up."""
        self._client = None
