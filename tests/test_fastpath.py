"""The rule layer that answers everyday commands without the model."""

from __future__ import annotations

import pytest
from conftest import ScriptedProvider
from fake_mail import FakeMailProvider, mail

from jarvis.core.fastpath import PROVIDER, match
from jarvis.rules import RulesConfig

FAST = {"ai": {"fast_path": True, "routes": {name: [{"provider": "anthropic"}] for name in ("conversation", "computer", "smart_home")}}}


def core_with_fastpath(make_core, **kw):
    """A core where the model would raise if it were ever asked."""
    model = ScriptedProvider("anthropic", "cloud", [AssertionError("the model must not be used for an everyday command")])
    kw.setdefault("computer", {"enabled": True, "dry_run": False})
    return make_core({"anthropic": model}, **FAST, **kw), model


# ------------------------------------------------------------------ matching ----
@pytest.mark.parametrize("text, tool, args", [
    ("öppna spotify", "computer_open_app", {"app": "spotify"}),
    ("Öppna CapCut", "computer_open_app", {"app": "capcut"}),
    ("starta discord", "computer_open_app", {"app": "discord"}),
    ("open spotify", "computer_open_app", {"app": "spotify"}),
    ("byt till chrome", "computer_window", {"title_contains": "chrome", "action": "focus"}),
    ("växla till capcut", "computer_window", {"title_contains": "capcut", "action": "focus"}),
    ("stäng anteckningar", "computer_window", {"title_contains": "anteckningar", "action": "close"}),
    ("volym 30", "volume_set", {"level": 30}),
    ("sätt volymen på 20", "volume_set", {"level": 20}),
    ("volymen till 55 %", "volume_set", {"level": 55}),
    ("volume 40", "volume_set", {"level": 40}),
    ("höj volymen", "volume_adjust", {"step": 10}),
    ("sänk volymen", "volume_adjust", {"step": -10}),
    ("tysta", "volume_mute", {"muted": True}),
    ("tysta ljudet", "volume_mute", {"muted": True}),
    ("ljudet på", "volume_mute", {"muted": False}),
    ("pausa", "media_control", {"action": "pause"}),
    ("pausa musiken", "media_control", {"action": "pause"}),
    ("nästa låt", "media_control", {"action": "next"}),
    ("föregående låt", "media_control", {"action": "previous"}),
    ("öppna youtube", "computer_open_web", {"site": "youtube"}),
    ("öppna svt.se", "computer_open_web", {"site": "svt.se"}),
    ("sök på väder stockholm", "computer_search_web", {"query": "väder stockholm"}),
    ("google kattbilder", "computer_search_web", {"query": "kattbilder"}),
    ("öppna hämtade filer", "computer_open_folder", {"folder": "hämtade filer"}),
    ("öppna skrivbordet", "computer_open_folder", {"folder": "skrivbordet"}),
    ("lås datorn", "power_lock", {}),
    ("vad är klockan?", "system_get_time", {}),
    ("sammanfatta dagen", "monitor_summary_now", {}),
])
def test_rules_that_fire(text, tool, args):
    call = match(text)
    assert call is not None and call.tool == tool and call.args == args


@pytest.mark.parametrize("text", [
    "berätta ett skämt", "vad tycker du om katter", "öppna", "hitta min senaste faktura",
    "har jag fått några viktiga mejl?", "kan du öppna spotify och sedan pausa musiken",
    "vad är klockan i Tokyo?", "varför är himlen blå", "", "   ",
    "skriv ett mejl till mamma", "radera gamla filer",
])
def test_anything_unclear_goes_to_the_model(text):
    assert match(text) is None


# -------------------------------------------------------------- through core ----
async def test_opening_a_program_never_reaches_the_model(make_core):
    core, model = core_with_fastpath(make_core)
    res = await core.orchestrator.handle("öppna spotify")
    assert res["ok"] and res["provider"] == PROVIDER and res["model"] == "regler"
    assert res["is_fallback"] is False and res["estimated_cost_usd"] == 0.0
    assert model.requests == []  # the model was never asked
    assert core.history.list()[0]["tool"] == "computer_open_app"


async def test_it_says_the_program_was_already_open(make_core):
    core, _ = core_with_fastpath(make_core)
    from jarvis.computer.windows import WindowInfo

    core.desktop.api.open_windows = [WindowInfo(handle=7, title="Projekt – CapCut", process_id=7)]
    res = await core.orchestrator.handle("öppna capcut")
    assert res["text"] == "CapCut var redan öppet, jag tog fram det."


async def test_it_does_not_claim_a_window_it_cannot_see(make_core):
    core, _ = core_with_fastpath(make_core)
    core.desktop.api.open_windows = []
    res = await core.orchestrator.handle("öppna spotify")
    assert res["text"] == "Jag startade Spotify, men ser inget fönster än."


async def test_a_started_program_with_a_window_is_reported_as_started(make_core):
    core, _ = core_with_fastpath(make_core)
    from jarvis.computer.windows import WindowInfo

    api = core.desktop.api
    api.open_windows = []
    original = core.launcher._launch

    def launch_and_show(command):
        original(command)
        api.open_windows = [WindowInfo(handle=3, title="Spotify Premium", process_id=3)]

    core.launcher._launch = launch_and_show
    res = await core.orchestrator.handle("öppna spotify")
    assert res["text"] == "Spotify är startat."


@pytest.mark.parametrize("text, expected", [
    ("volym 25", "Volymen är nu 25 procent."),
    ("pausa musiken", "Pausade musiken."),
    ("nästa låt", "Nästa låt."),
    ("tysta", "Ljudet är tystat."),
    ("lås datorn", "Låser datorn."),
    ("öppna youtube", "Öppnade https://www.youtube.com."),
])
async def test_replies_come_from_the_tool_result(make_core, text, expected):
    core, _ = core_with_fastpath(make_core)
    res = await core.orchestrator.handle(text)
    assert res["text"] == expected


async def test_a_failing_tool_is_reported_not_papered_over(make_core):
    core, _ = core_with_fastpath(make_core)
    res = await core.orchestrator.handle("öppna finnsintealls")
    assert "Hittade inget installerat program" in res["text"]
    assert core.history.list()[0]["status"] == "failed"


async def test_dry_run_is_honest_about_doing_nothing(make_core):
    core, _ = core_with_fastpath(make_core, computer={"enabled": True, "dry_run": True})
    res = await core.orchestrator.handle("öppna spotify")
    assert res["text"] == "Skulle ha startat Spotify (dry-run, inget hände)."
    assert core.launcher._launch is not None and core.desktop.api.actions == []


# ------------------------------------------------------- same rules as always ----
async def test_the_shortcut_still_obeys_the_untrusted_data_rules(make_core):
    """A command typed after JARVIS read an e-mail still has to be confirmed."""
    evil = mail("m1", "X <x@exempel.se>", "Hej")
    rules = RulesConfig.model_validate({"mail": {"important_domains": ["exempel.se"]}})
    core, _ = core_with_fastpath(make_core, rules=rules, mail_provider=FakeMailProvider([evil]))
    await core.mail.poll()
    model = ScriptedProvider("anthropic", "cloud", [[("mail_list_recent", {})], "Du har ett mejl."])
    core.router.providers["anthropic"] = model
    await core.orchestrator.handle("har jag fått mejl?")

    res = await core.orchestrator.handle("öppna spotify")  # the shortcut, after reading mail
    assert res["provider"] == PROVIDER
    h = [x for x in core.history.list() if x["tool"] == "computer_open_app"][0]
    assert h["status"] == "denied" and "Efter extern data" in h["summary"]


async def test_the_shortcut_is_off_for_a_profile_that_may_not_act(make_core):
    core, _ = core_with_fastpath(make_core)
    await core.update_settings({"active_profile": "guest"})
    model = ScriptedProvider("anthropic", "cloud", ["Jag får inte göra det."])
    core.router.providers["anthropic"] = model
    res = await core.orchestrator.handle("öppna spotify")
    assert res["provider"] != PROVIDER  # no tool for this profile: the model answers instead
    assert core.history.list() == []


async def test_the_shortcut_can_be_turned_off(make_core):
    model = ScriptedProvider("anthropic", "cloud", [[("computer_open_app", {"app": "spotify"})], "Startade."])
    core = make_core({"anthropic": model}, computer={"enabled": True, "dry_run": True},
                     ai={"fast_path": False, "routes": {"conversation": [{"provider": "anthropic"}], "computer": [{"provider": "anthropic"}]}})
    res = await core.orchestrator.handle("öppna spotify")
    assert res["provider"] == "anthropic" and model.requests
