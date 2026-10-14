"""Per-request tool selection: fewer tools shown to the model, same security."""

from __future__ import annotations

import pytest
from conftest import ScriptedProvider

from jarvis.core.tool_select import detect_groups, select_tools, tool_group

CLOUD = {"ai": {"routes": {"conversation": [{"provider": "anthropic"}], "computer": [{"provider": "anthropic"}], "smart_home": [{"provider": "anthropic"}]}}}


@pytest.mark.parametrize(
    "text, groups",
    [
        ("Vad är klockan?", {"system"}),
        ("Visa systemstatus", {"system"}),
        ("Vilka program får du starta?", {"computer"}),
        ("Öppna anteckningar", {"computer"}),
        ("Pausa musiken i köket", {"smart_home"}),
        ("Which speakers do you have?", {"smart_home"}),
        ("hitta min senaste faktura", {"files"}),
        ("öppna mappen där den ligger", {"computer", "files"}),
        ("har jag fått några viktiga mejl?", {"mail", "monitor"}),
        ("sammanfatta dagen", {"monitor"}),
        ("vad har hänt idag?", {"monitor"}),
        ("Berätta ett kort skämt", set()),
    ],
)
def test_detect_groups(text, groups):
    assert detect_groups(text) == groups


def test_every_registered_tool_has_a_known_group(make_core):
    core = make_core()
    groups = {tool_group(t) for t in core.registry.all()}
    assert groups <= {"system", "computer", "files", "mail", "monitor", "smart_home"}
    assert tool_group(core.registry.get("rules_show")) == "system"


def test_system_and_rules_always_offered_unknown_offers_all(make_core):
    tools = make_core().registry.all()
    names = lambda ts: {t.name for t in ts}  # noqa: E731
    files_only, groups = select_tools("hitta min senaste faktura", tools)
    assert groups == {"files"}
    assert {"files_search", "files_open_folder", "system_get_time", "system_get_status", "rules_show"} <= names(files_only)
    assert not any(n.startswith(("sonos_", "computer_", "mail_")) for n in names(files_only))
    clock, _ = select_tools("Vad är klockan?", tools)
    assert names(clock) == {t.name for t in tools if tool_group(t) == "system"}
    everything, groups = select_tools("Berätta ett kort skämt", tools)
    assert groups is None and names(everything) == names(tools)


async def test_turn_keeps_the_same_tools_in_every_round(make_core, tmp_path):
    model = ScriptedProvider("anthropic", "cloud", [[("files_search", {"query": "faktura"})], [("system_get_time", {})], "Klart."])
    core = make_core({"anthropic": model}, **CLOUD)
    r = await core.orchestrator.handle("hitta min senaste faktura")
    assert r["ok"] and r["tool_groups"] == ["files"]
    sent = [req["tools"] for req in model.requests]
    assert len(sent) == 3 and sent[0] == sent[1] == sent[2]
    assert "files_search" in sent[0] and "sonos_playback" not in sent[0]


async def test_executor_still_checks_calls_outside_the_offered_tools(make_core):
    # The model calls a tool it was not shown: it still goes through validation,
    # permissions, confirmation and dry-run like any other call.
    model = ScriptedProvider("anthropic", "cloud", [[("computer_open_app", {"app": "notepad"}), ("no_such_tool", {})], "Ok."])
    core = make_core({"anthropic": model}, **CLOUD)
    await core.update_settings({"dry_run_computer": True})
    r = await core.orchestrator.handle("Vad är klockan?")
    assert r["ok"] and "computer_open_app" not in model.requests[0]["tools"]
    hist = {h["tool"]: h for h in core.history.list()}
    assert hist["computer_open_app"]["dry_run"] is True  # dry-run still enforced
    assert hist["no_such_tool"]["status"] == "failed"  # unknown tools are still rejected


async def test_guest_role_permissions_unchanged(make_core):
    model = ScriptedProvider("anthropic", "cloud", [[("computer_open_app", {"app": "notepad"})], "Ok."])
    core = make_core({"anthropic": model}, **CLOUD)
    await core.update_settings({"active_profile": "guest", "dry_run_computer": False})
    await core.orchestrator.handle("öppna anteckningar")
    h = core.history.list()[0]
    assert h["tool"] == "computer_open_app" and h["status"] == "denied"
