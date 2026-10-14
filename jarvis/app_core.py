"""Composition root: builds every component and exposes one state snapshot."""

from __future__ import annotations

import asyncio
import logging
import sys
import time
from typing import Any

import httpx

from . import __version__
from .computer.apps import AppLauncher
from .computer.desktop import Desktop
from .computer.tools import register_computer_tools
from .computer.tools_desktop import register_desktop_tools
from .computer.shell import PowerShellRunner
from .computer.userfiles import FileActions
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
from .monitoring.gaming import GamingDetector, GamingMode
from .monitoring.notifier import NotificationChannel
from .monitoring.service import MonitorService, register_monitor_tools
from .rules import RulesConfig, load_rules, register_rules_tools
from .system_tools import register_system_tools
from .voice.interfaces import VoiceController

log = logging.getLogger(__name__)

PRIVACY_KEYS = {"local_only", "cloud_max_sensitivity"}


class JarvisCore:
    HEALTH_INTERVAL_S = 30.0
    # Right after login Ollama may still be starting: check more often for a while, and say so.
    OLLAMA_STARTUP_WINDOW_S = 300.0
    OLLAMA_STARTUP_INTERVAL_S = 5.0

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
        launcher: AppLauncher | None = None,
        desktop: Desktop | None = None,
        file_actions: FileActions | None = None,
        shell: PowerShellRunner | None = None,
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
            on_change=lambda: self.bus.publish("monitoring", self.monitoring_view()),
        )
        self.monitor.notifier.paused = self.settings.current.monitoring_paused
        # Game mode: no background model calls while a game runs or the GPU is busy.
        self.gaming = GamingMode(
            GamingDetector(cfg.gaming), on_start=self._gaming_started, on_end=self._gaming_ended, on_change=self.monitor._changed
        )
        self.monitor.gaming = self.gaming

        self.registry = ToolRegistry()
        register_system_tools(self.registry, self)
        register_rules_tools(self.registry, lambda: self.rules)
        register_monitor_tools(self.registry, self.monitor)
        self.mail: MailWatcher | None = None
        if cfg.mail.enabled:
            provider = mail_provider if mail_provider is not None else GmailProvider(query=cfg.mail.query)
            # Mail is classified by the LOCAL model only: the Ollama provider object itself,
            # never the router, so routes or cloud providers can never be used for mail.
            self.mail = MailWatcher(
                cfg.mail, provider, self.monitor, lambda: self.rules, lambda: self.router.providers.get("ollama"), is_gaming=lambda: self.gaming.active
            )
            register_mail_tools(self.registry, self.mail)
            self.monitor.add_digest("mail", self.mail.important_since)
        self.files: FileWatcher | None = None
        if cfg.files.enabled:
            # Metadata-only index of the watched folders (data/files.db); contents are never read.
            self.files = FileWatcher(cfg.files, self.monitor, lambda: self.rules, data / "files.db" if data else None)
            # Opening Explorer is desktop control: only where computer control is enabled.
            register_file_tools(self.registry, self.files, allow_open=self.windows.enabled)
            self.monitor.add_digest("files", self.files.take_new)
        if cfg.homeassistant.enabled and cfg.homeassistant.url:
            # Without a Home Assistant URL these tools can only fail, and every extra
            # tool makes the local model's choice worse. Registered once it is configured.
            register_sonos_tools(self.registry, self.sonos, cfg.homeassistant)
        if self.windows.enabled:
            # Desktop control is platform-specific; a headless backend (e.g. a
            # Raspberry Pi) runs without it and never offers these tools.
            # One AppLauncher/Desktop/FileActions is shared, so "ångra" undoes
            # the file action JARVIS actually did.
            # Injectable so tests never read the real Start menu or touch the real desktop.
            self.launcher = launcher or AppLauncher(cfg.computer)
            self.desktop = desktop or Desktop()
            self.file_actions = file_actions or FileActions()
            register_computer_tools(self.registry, self.windows, launcher=self.launcher, desktop=self.desktop, files=self.file_actions)
            register_desktop_tools(
                self.registry, desktop=self.desktop, files=self.file_actions, shell=shell,
                advanced_mode=lambda: self.settings.current.advanced_mode,
            )

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
        channels: list[NotificationChannel] = [UIChannel(self._publish_if_owner)]
        want_windows = self.cfg.monitoring.windows_notifications
        if want_windows is None:
            want_windows = sys.platform == "win32"
        if want_windows:
            try:
                channels.append(WindowsToastChannel())
            except RuntimeError as exc:
                log.info("Windows notifications unavailable: %s", exc)
        return channels

    # -- owner-only views -------------------------------------------------------
    def owner_active(self) -> bool:
        profile = self.cfg.profile(self.settings.current.active_profile)
        return profile is not None and profile.role == "owner"

    def monitoring_view(self) -> dict[str, Any]:
        """Notifications and the summary are personal: only shown for the owner profile."""
        if self.owner_active():
            return self.monitor.snapshot()
        return {"restricted": True, "detail": "Notiser och sammanfattning visas bara för ägarprofilen."}

    def _publish_if_owner(self, event: str, data: dict[str, Any]) -> None:
        if self.owner_active():
            self.bus.publish(event, data)

    async def _gaming_started(self) -> None:
        ollama = self.router.providers.get("ollama")
        if ollama is not None and hasattr(ollama, "unload"):
            await ollama.unload()  # give the GPU back to the game

    async def _gaming_ended(self) -> None:
        if self.mail is not None and self.mail.waiting:
            await self.mail.classify_waiting()

    def _privacy(self) -> PrivacyConfig:
        st = self.settings.current
        return self.cfg.privacy.model_copy(update={"local_only": st.local_only, "cloud_max_sensitivity": st.cloud_max_sensitivity})

    # -- lifecycle -----------------------------------------------------------
    async def start(self) -> None:
        await self.refresh_integrations()
        self._bg = asyncio.create_task(self._health_loop(), name="jarvis-health")
        if getattr(self, "launcher", None) is not None:
            # Read the Start menu once in the background, so the first "öppna spotify" is instant.
            asyncio.create_task(asyncio.to_thread(self.launcher.apps), name="jarvis-app-index")
        self._state_watch = asyncio.create_task(self._push_state_after_actions(), name="jarvis-state-watch")
        if self.cfg.monitoring.enabled:
            await self.monitor.start()
            if self.cfg.gaming.enabled:
                await self.gaming.poll()  # before the first mail check, so a running game is respected
                self.monitor.scheduler.every("gaming", self.cfg.gaming.check_seconds, self.gaming.poll)
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

    def _ollama_starting(self, providers: list[dict[str, Any]]) -> bool:
        """Shortly after start, an unreachable Ollama is most likely still starting."""
        if time.time() - self.started_at > self.OLLAMA_STARTUP_WINDOW_S:
            return False
        return any(p["name"] == "ollama" and not p["available"] for p in providers)

    async def _health_loop(self) -> None:
        while True:
            starting = self._ollama_starting(await self.router.status())
            await asyncio.sleep(self.OLLAMA_STARTUP_INTERVAL_S if starting else self.HEALTH_INTERVAL_S)
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
        if self._ollama_starting(providers):
            for p in providers:
                if p["name"] == "ollama" and not p["available"]:
                    p["detail"] = "Ollama startar… (försöker igen var 5:e sekund)"
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
            "monitoring": self.monitoring_view(),
            "camera": {"state": "not_implemented", "master_switch": "on" if st.camera_enabled else "off"},
            "computer": {**self.windows.status(), "dry_run": st.dry_run_computer},
            "settings": st.model_dump(),
            "profiles": [p.model_dump() for p in self.cfg.profiles],
            "active_profile": profile.model_dump() if profile else None,
            "pending_confirmations": self.confirmations.pending(),
            "conversation": {
                "id": self.orchestrator.conversation.id,
                "busy": self.orchestrator.busy,
                # Shown in the UI: the chat holds text written by someone else, so
                # actions are confirmed until it is cleared.
                "untrusted_context": sorted(self.orchestrator.conversation.untrusted_sources()),
            },
        }
