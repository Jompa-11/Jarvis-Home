"""Personal tools and the notification/summary API are for the owner profile only."""

from __future__ import annotations

import pytest
from conftest import ScriptedProvider
from fastapi.testclient import TestClient

from jarvis.providers.dev_provider import DevProvider
from jarvis.server.app import create_app

PERSONAL = ["mail_list_recent", "files_search", "files_open_folder", "monitor_status", "monitor_summary_now", "rules_show"]
CLOUD = {"ai": {"routes": {"conversation": [{"provider": "anthropic"}], "computer": [{"provider": "anthropic"}], "smart_home": [{"provider": "anthropic"}]}}}
TOKEN = "t" * 32
H = {"X-Jarvis-Token": TOKEN}


def test_personal_tools_have_their_own_category(make_core):
    core = make_core()
    assert {core.registry.get(n).category for n in PERSONAL} == {"personal"}


@pytest.mark.parametrize("profile, role", [("kid", "child"), ("guest", "guest")])
async def test_child_and_guest_are_denied_personal_tools(make_core, profile, role):
    model = ScriptedProvider("anthropic", "cloud", [[(n, {"path": "x"} if n == "files_open_folder" else {}) for n in PERSONAL], "Ok."])
    core = make_core({"anthropic": model}, **CLOUD)
    await core.update_settings({"active_profile": profile})
    offered = {t.name for t in core.policy.tools_for_role(role, core.registry.all())}
    assert offered.isdisjoint(PERSONAL)  # never even shown to the model
    await core.orchestrator.handle("vad har hänt idag?")
    statuses = {h["tool"]: h["status"] for h in core.history.list()}
    assert statuses == {n: "denied" for n in PERSONAL}  # and refused if called anyway


async def test_owner_is_allowed_personal_tools(make_core):
    model = ScriptedProvider("anthropic", "cloud", [[("rules_show", {}), ("monitor_status", {}), ("files_search", {"query": "x"}), ("monitor_summary_now", {})], "Ok."])
    core = make_core({"anthropic": model}, **CLOUD)
    await core.orchestrator.handle("vad har hänt idag?")
    assert {h["tool"]: h["status"] for h in core.history.list()} == {
        "rules_show": "succeeded", "monitor_status": "succeeded", "files_search": "succeeded", "monitor_summary_now": "succeeded",
    }


async def test_open_folder_still_follows_the_computer_dry_run(make_core, tmp_path):
    (tmp_path / "watched").mkdir()
    (tmp_path / "watched" / "testfaktura.pdf").write_bytes(b"x")
    model = ScriptedProvider("anthropic", "cloud", [[("files_open_folder", {"path": "testfaktura.pdf"})], "Ok."])
    core = make_core({"anthropic": model}, **CLOUD)
    await core.files.refresh()
    await core.update_settings({"dry_run_computer": True})
    await core.orchestrator.handle("öppna mappen")
    h = core.history.list()[0]
    assert h["status"] == "succeeded" and h["dry_run"] is True


@pytest.fixture
def client(make_core):
    providers = {
        "anthropic": ScriptedProvider("anthropic", "cloud", [], available=False),
        "ollama": ScriptedProvider("ollama", "local", [], available=False),
        "dev": DevProvider(),
    }
    core = make_core(providers)
    with TestClient(create_app(core, token=TOKEN, extra_hosts={"testserver"})) as c:
        c.core = core
        yield c


CALLS = [
    ("get", "/api/monitoring", None),
    ("post", "/api/monitoring/pause", {"paused": True}),
    ("post", "/api/notifications/mute", {"minutes": 10}),
    ("post", "/api/monitoring/summary", {}),
    ("post", "/api/notifications/ack", {}),
]


@pytest.mark.parametrize("profile", ["kid", "guest"])
def test_notification_api_is_owner_only(client, profile):
    assert client.patch("/api/settings", json={"active_profile": profile}, headers=H).status_code == 200
    for method, url, body in CALLS:
        r = getattr(client, method)(url, headers=H, **({"json": body} if body is not None else {}))
        assert r.status_code == 403, url
    state = client.get("/api/state", headers=H).json()
    assert state["monitoring"]["restricted"] is True and "recent" not in state["monitoring"]
    assert client.core.monitor.paused is False  # nothing changed


def test_notification_api_works_for_owner(client):
    for method, url, body in CALLS:
        r = getattr(client, method)(url, headers=H, **({"json": body} if body is not None else {}))
        assert r.status_code == 200, url
    assert "recent" in client.get("/api/state", headers=H).json()["monitoring"]


async def test_ui_channel_publishes_only_for_owner(make_core):
    core = make_core()
    events = []
    core.bus.publish = lambda t, d: events.append(t)
    core._publish_if_owner("notification", {})
    await core.update_settings({"active_profile": "guest"})
    events.clear()
    core._publish_if_owner("notification", {})
    assert events == []
