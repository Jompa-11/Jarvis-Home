from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fake_ha import create_fake_ha  # noqa: E402
from fake_mail import FakeMailProvider  # noqa: E402

from jarvis.app_core import JarvisCore  # noqa: E402
from jarvis.computer.apps import App, AppLauncher  # noqa: E402
from jarvis.computer.desktop import Desktop  # noqa: E402
from jarvis.computer.shell import PowerShellRunner  # noqa: E402
from jarvis.computer.userfiles import FileActions  # noqa: E402
from jarvis.config import JarvisConfig  # noqa: E402
from jarvis.providers.base import AIProvider, ProviderHealth, ProviderResponse, ToolCall, Usage  # noqa: E402
from jarvis.rules import RulesConfig  # noqa: E402

HA_TOKEN = "test-token-abcdef123456"


@pytest.fixture(autouse=True)
def _isolated_env(monkeypatch):
    # Never touch real credentials or services from tests.
    for var in ("ANTHROPIC_API_KEY", "HA_TOKEN", "HA_URL", "JARVIS_CONFIG", "JARVIS_PORT", "JARVIS_HOST"):
        monkeypatch.delenv(var, raising=False)


class ScriptedProvider(AIProvider):
    """A fake model that returns pre-scripted responses and records requests."""

    def __init__(self, name: str = "scripted", kind: str = "cloud", script: list[Any] | None = None, available: bool = True):
        self.name = name
        self.kind = kind  # type: ignore[assignment]
        self.script = list(script or [])
        self.available = available
        self.requests: list[dict[str, Any]] = []

    async def health(self) -> ProviderHealth:
        return ProviderHealth(self.available, "scripted")

    async def complete(self, *, system, messages, tools, model, effort, timeout_s) -> ProviderResponse:
        self.requests.append({"system": system, "messages": list(messages), "tools": [t.name for t in tools], "model": model})
        if not self.script:
            return ProviderResponse("Done.", [], "end_turn", Usage(10, 5), self.name, model or "m")
        step = self.script.pop(0)
        if isinstance(step, Exception):
            raise step
        if callable(step):
            step = step(messages)
        if isinstance(step, str):
            return ProviderResponse(step, [], "end_turn", Usage(10, 5), self.name, model or "m")
        calls = [ToolCall(id=f"call_{i}_{len(self.requests)}", name=n, arguments=a) for i, (n, a) in enumerate(step)]
        return ProviderResponse("", calls, "tool_use", Usage(10, 5), self.name, model or "m")


FAKE_APPS = [
    App("Spotify", "SpotifyAB.SpotifyMusic_x!Spotify"),
    App("Discord", "com.squirrel.Discord.Discord"),
    App("Anteckningar", "Microsoft.WindowsNotepad_x!App"),
    App("Google Chrome", "Chrome"),
    App("Kalkylatorn", "Microsoft.WindowsCalculator_x!App"),
]


class FakeDesktopAPI:
    """A desktop that records what it was asked to do instead of doing it."""

    supported = True

    def __init__(self) -> None:
        self.volume, self.muted = 40, False
        self.actions: list[tuple] = []
        self.clipboard = ""

    def get_volume(self) -> tuple[int, bool]:
        return self.volume, self.muted

    def set_volume(self, percent: int) -> None:
        self.volume = percent
        self.actions.append(("volume", percent))

    def set_mute(self, muted: bool) -> None:
        self.muted = muted
        self.actions.append(("mute", muted))

    def press_keys(self, vks: list[int]) -> None:
        self.actions.append(("keys", list(vks)))

    def type_text(self, text: str) -> None:
        self.actions.append(("type", text))

    def get_clipboard(self) -> str:
        return self.clipboard

    def set_clipboard(self, text: str) -> None:
        self.clipboard = text
        self.actions.append(("clipboard", text))

    def active_window_title(self) -> str:
        return "Testfönster"

    def window_action(self, hwnd: int, action: str) -> None:
        self.actions.append(("window", hwnd, action))

    def lock_workstation(self) -> None:
        self.actions.append(("lock",))

    def power(self, action: str) -> None:
        self.actions.append(("power", action))


def _no_shell(*a, **kw):
    raise AssertionError("a test must never run a real PowerShell command")


def make_config(tmp_path: Path, **overrides: Any) -> JarvisConfig:
    raw: dict[str, Any] = {
        "data_dir": str(tmp_path / "data"),
        "ai": {"routes": {"conversation": [{"provider": "anthropic", "model": "claude-opus-5-5"}, {"provider": "ollama"}, {"provider": "dev"}]}},
        # Tests exercise real (fake-HA) calls; dry-run behaviour is tested explicitly.
        "homeassistant": {"url": "http://ha.test", "timeout_s": 2, "dry_run": False},
        # Dry-run ON in tests (the product default is off): a test must never really
        # launch a program or open Explorer. Tests that check real execution use fakes.
        "computer": {"enabled": True, "dry_run": True},
        # Never index the owner's real Downloads/Documents from tests.
        "files": {"watched_folders": [str(tmp_path / "watched")]},
        # Never look at the real process list or GPU from tests (game mode is tested with fakes).
        "gaming": {"enabled": False},
        "profiles": [
            {"id": "owner", "name": "Alex", "role": "owner"},
            {"id": "kid", "name": "Sam", "role": "child"},
            {"id": "guest", "name": "Guest", "role": "guest"},
        ],
    }
    for k, v in overrides.items():
        if isinstance(v, dict) and isinstance(raw.get(k), dict):
            raw[k] = {**raw[k], **v}
        else:
            raw[k] = v
    return JarvisConfig.model_validate(raw)


@pytest.fixture
def fake_ha():
    return create_fake_ha(HA_TOKEN)


@pytest.fixture
def make_core(tmp_path, fake_ha, monkeypatch):
    created: list[JarvisCore] = []

    def _make(providers: dict[str, AIProvider] | None = None, *, ha_token: bool = True, rules: RulesConfig | None = None, mail_provider: Any = None, shell: Any = None, **cfg_overrides) -> JarvisCore:
        if ha_token:
            monkeypatch.setenv("HA_TOKEN", HA_TOKEN)
        cfg = make_config(tmp_path, **cfg_overrides)
        core = JarvisCore(
            cfg,
            providers=providers,
            ha_transport=httpx.ASGITransport(app=fake_ha),
            persist=False,
            sonos_verify_delay_s=0.01,
            # Never read the owner's real config/rules.toml from tests.
            rules=rules if rules is not None else RulesConfig(),
            # Never show real Windows notifications from tests.
            notification_channels=[],
            # Never touch Gmail or Windows Credential Manager from tests.
            mail_provider=mail_provider if mail_provider is not None else FakeMailProvider(signed_in=False),
            # Never read the real Start menu, touch the real desktop or run PowerShell from a test.
            launcher=AppLauncher(cfg.computer, list_apps=lambda: list(FAKE_APPS), launch=lambda cmd: None),
            desktop=Desktop(api=FakeDesktopAPI()),
            file_actions=FileActions(home=tmp_path / "home", notes_folder=tmp_path / "home" / "Documents", recycle=lambda p: p.unlink()),
            shell=shell if shell is not None else PowerShellRunner(run=_no_shell),
        )
        core.sonos._verify_timeout_s = 0.3
        # A missing approval must fail the test quickly, not hang the whole suite.
        core.confirmations._timeout_s = 2.0
        created.append(core)
        return core

    yield _make
