"""Composition root: builds every component and exposes one state snapshot."""

from __future__ import annotations

import asyncio
import logging
import sys
import time
from typing import Any

import httpx

from . import __version__
from .computer.tools import register_computer_tools
from .computer.windows import WindowsControl
from .config import JarvisConfig, PrivacyConfig
from .core.events import EventBus
from .core.executor import ToolExecutor, ToolHistory
from .core.orchestrator import Orchestrator
from .core.permissions import ConfirmationManager, PermissionPolicy
from .core.settings import SettingsStore
from .core.tools import ToolRegistry
from .integrations.homeassistant.client import HAError, HomeAssistantClient
from .integrations.homeassistant.sonos import SonosError, SonosService
from .integrations.homeassistant.tools import register_sonos_tools
from .providers.anthropic_provider import AnthropicProvider
from .providers.base import AIProvider
from .providers.dev_provider import DevProvider
from .providers.ollama_provider import OllamaProvider
from .providers.router import ProviderRouter
from .providers.usage import UsageTracker
from .files.watcher import FileWatcher, register_file_tools
from .mail.base import MailProvider
from .mail.gmail import GmailProvider
from .mail.watcher import MailWatcher, register_mail_tools
from .monitoring.channels import UIChannel, WindowsToastChannel
from .monitoring.notifier import NotificationChannel
from .monitoring.service import MonitorService, register_monitor_tools
from .rules import RulesConfig, load_rules, register_rules_tools
from .system_tools import register_system_tools
from .voice.interfaces import VoiceController

log = logging.getLogger(__name__)

PRIVACY_KEYS = {"local_only", "cloud_max_sensitivity"}


class JarvisCore:
    HEALTH_INTERVAL_S = 30.0

    def __init__(
        self,
        cfg: JarvisConfig,
        *,
        providers: dict[str, AIProvider] | None = None,
        ha_transport: httpx.AsyncBaseTransport | None = None,
        persist: bool = True,
        sonos_verify_delay_s: float = 0.5,
        rules: RulesConfig | None = None,
        notification_channels: list[NotificationChannel] | None = None,
        mail_provider: MailProvider | None = None,
    ) -> None:
        self.cfg = cfg
        self.rules = rules if rules is not None else load_rules()
        self.started_at = time.time()
        data = cfg.data_dir if persist else None
        self.bus = EventBus()
        self.settings = SettingsStore(cfg, data / "settings.json" if data else None)
        self.usage = UsageTracker(data / "usage.json" if data else None, cfg.ai.pricing, cfg.ai.monthly_budget_usd)
        providers = providers or {
            "anthropic": AnthropicProvider(cfg.anthropic),
            "ollama": OllamaProvider(cfg.ollama),
            "dev": DevProvider(),
        }
        self.router = ProviderRouter(providers, cfg.ai, self._privacy(), self.usage)

        self.ha = HomeAssistantClient(cfg.homeassistant, transport=ha_transport)
        self.sonos = SonosService(self.ha, verify_delay_s=sonos_verify_delay_s)
        self.windows = WindowsControl(cfg.computer)
        self.voice = VoiceController()
        self.voice.set_enabled(self.settings.current.microphone_enabled)

        if notification_channels is None:
            notification_channels = self._default_channels()
        self.monitor = MonitorService(
            cfg.monitoring,
            lambda: self.rules,
            channels=notification_channels,
            state_path=data / "monitor_state.json" if data else None,
            on_change=lambda: self.bus.publish("monitoring", self.monitor.snapshot()),
        )
        self.monitor.notifier.paused = self.settings.current.monitoring_paused

        self.registry = ToolRegistry()
        register_system_tools(self.registry, self)
        register_rules_tools(self.registry, lambda: self.rules)
        register_monitor_tools(self.registry, self.monitor)
        self.mail: MailWatcher | None = None
        if cfg.mail.enabled:
            provider = mail_provider if mail_provider is not None else GmailProvider(query=cfg.mail.query)
            # Mail is classified by the LOCAL model only: the Ollama provider object itself,
            # never the router, so routes or cloud providers can never be used for mail.
            self.mail = MailWatcher(cfg.mail, provider, self.monitor, lambda: self.rules, lambda: self.router.providers.get("ollama"))
            register_mail_tools(self.registry, self.mail)
            self.monitor.add_digest("mail", self.mail.important_since)
        self.files: FileWatcher | None = None
        if cfg.files.enabled:
            # Metadata-only index of the watched folders (data/files.db); contents are never read.
            self.files = FileWatcher(cfg.files, self.monitor, lambda: self.rules, data / "files.db" if data else None)
            # Opening Explorer is desktop control: only where computer control is enabled.
            register_file_tools(self.registry, self.files, allow_open=self.windows.enabled)
            self.monitor.add_digest("files", self.files.take_new)
        register_sonos_tools(self.registry, self.sonos, cfg.homeassistant)
        if self.windows.enabled:
            # Desktop control is platform-specific; a headless backend (e.g. a
            # Raspberry Pi) runs without it and never offers these tools.
            register_computer_tools(self.registry, self.windows)

        self.policy = PermissionPolicy()
        self.confirmations = ConfirmationManager(self.bus)
        self.history = ToolHistory(cfg.retention.tool_history_size)
        self.executor = ToolExecutor(self.registry, self.policy, self.confirmations, self.history, self.bus)
        self.orchestrator = Orchestrator(cfg, self.settings, self.router, self.registry, self.executor, self.policy, self.bus, rules=self.rules)

        self.sonos_error: str | None = None
        self.diagnostics: dict[str, Any] | None = None
        self._bg: asyncio.Task | None = None
        self._state_watch: asyncio.Task | None = None

    def _default_channels(self) -> list[NotificationChannel]:
        channels: list[NotificationChannel] = [UIChannel(self.bus.publish)]
        want_windows = self.cfg.monitoring.windows_notifications
        if want_windows is None:
            want_windows = sys.platform == "win32"
        if want_windows:
            try:
                channels.append(WindowsToastChannel())
            except RuntimeError as exc:
                log.info("Windows notifications unavailable: %s", exc)
        return channels

    def _privacy(self) -> PrivacyConfig:
        st = self.settings.current
        return self.cfg.privacy.model_copy(update={"local_only": st.local_only, "cloud_max_sensitivity": st.cloud_max_sensitivity})

    # -- lifecycle -----------------------------------------------------------
    async def start(self) -> None:
        await self.refresh_integrations()
        self._bg = asyncio.create_task(self._health_loop(), name="jarvis-health")
        self._state_watch = asyncio.create_task(self._push_state_after_actions(), name="jarvis-state-watch")
        if self.cfg.monitoring.enabled:
            await self.monitor.start()
            if self.mail is not None:
                self.mail._publish_state()
                if self.mail.provider.signed_in():
                    self.monitor.scheduler.every("mail", self.cfg.mail.poll_minutes * 60, self.mail.poll, run_now=True)
            if self.files is not None:
                # Runs as a background job; the scan itself is in a worker thread.
                self.monitor.scheduler.every("files", self.cfg.files.refresh_minutes * 60, self.files.refresh, run_now=True)
        log.info("JARVIS core started")

    async def stop(self) -> None:
        self.orchestrator.cancel()
        await self.monitor.stop()
        if self.mail is not None:
            await self.mail.provider.aclose()
        if self.files is not None:
            self.files.index.close()
        for task in (self._bg, self._state_watch):
            if task:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        await self.ha.aclose()
        for p in self.router.providers.values():
            try:
                await p.aclose()
            except Exception:
                pass
        log.info("JARVIS core stopped")

    async def _health_loop(self) -> None:
        while True:
            await asyncio.sleep(self.HEALTH_INTERVAL_S)
            try:
                await self.refresh_integrations()
            except Exception:
                log.exception("Health refresh failed")

    async def _push_state_after_actions(self) -> None:
        # After a tool finishes, publish a fresh snapshot so panels such as the
        # audio zones show the device state the action produced.
        queue = self.bus.subscribe()
        try:
            while True:
                ev = await queue.get()
                if ev["type"] == "tool.updated" and ev["data"].get("finished_at"):
                    self.bus.publish("state", await self.snapshot())
        finally:
            self.bus.unsubscribe(queue)

    async def refresh_integrations(self) -> None:
        await self.ha.check()
        if self.ha.status.state == "connected":
            try:
                await self.sonos.discover(force=True)
                self.sonos_error = None
            except (HAError, SonosError) as exc:
                self.sonos_error = str(exc)
        for name in self.router.providers:
            await self.router.health(name, force=True)
        self.bus.publish("state", await self.snapshot())

    # -- settings ------------------------------------------------------------
    async def update_settings(self, patch: dict[str, Any]) -> dict[str, Any]:
        st, changed = self.settings.update(patch)
        if "microphone_enabled" in changed:
            self.voice.set_enabled(st.microphone_enabled)
        if "monitoring_paused" in changed:
            self.monitor.set_paused(st.monitoring_paused)
        if changed & PRIVACY_KEYS:
            self.router.set_privacy(self._privacy())
        if changed & (PRIVACY_KEYS | {"active_profile"}):
            # A different profile or privacy policy starts a fresh conversation,
            # so earlier context is never reinterpreted under new rules.
            self.orchestrator.reset_conversation()
        self.bus.publish("state", await self.snapshot())
        return st.model_dump()

    # -- state -----------------------------------------------------------------
    async def snapshot(self) -> dict[str, Any]:
        st = self.settings.current
        providers = await self.router.status()
        chain = self.cfg.ai.routes["conversation"]
        speakers = [s.public() for s in self.sonos.cached_speakers()]
        ha_state = self.ha.status.state
        if ha_state != "connected":
            sonos_state, sonos_detail = "unavailable", "Requires a Home Assistant connection."
        elif self.sonos_error:
            sonos_state, sonos_detail = "error", self.sonos_error
        elif not speakers:
            sonos_state, sonos_detail = "no_speakers", "No Sonos speakers found in Home Assistant."
        else:
            sonos_state, sonos_detail = "ok", f"{len(speakers)} speaker(s) discovered."
        profile = self.cfg.profile(st.active_profile)
        return {
            "core": {"version": __version__, "started_at": self.started_at, "status": "online"},
            "ai": {
                "status": self.orchestrator.status,
                "last_error": self.orchestrator.last_error,
                "primary": {"provider": chain[0].provider, "model": chain[0].model},
                "providers": providers,
                "usage": self.usage.summary(),
            },
            "integrations": {
                "homeassistant": self.ha.status.public(),
                "sonos": {"state": sonos_state, "detail": sonos_detail, "speakers": speakers},
            },
            "voice": self.voice.status(),
            "monitoring": self.monitor.snapshot(),
            "camera": {"state": "not_implemented", "master_switch": "on" if st.camera_enabled else "off"},
            "computer": {**self.windows.status(), "dry_run": st.dry_run_computer},
            "settings": st.model_dump(),
            "profiles": [p.model_dump() for p in self.cfg.profiles],
            "active_profile": profile.model_dump() if profile else None,
            "pending_confirmations": self.confirmations.pending(),
            "conversation": {"id": self.orchestrator.conversation.id, "busy": self.orchestrator.busy},
        }
