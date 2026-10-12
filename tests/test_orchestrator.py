import asyncio
import json

from conftest import ScriptedProvider

from jarvis.providers.base import ProviderError
from jarvis.providers.dev_provider import DevProvider


def providers(script, **kw):
    return {
        "anthropic": ScriptedProvider("anthropic", "cloud", script, available=kw.get("cloud", True)),
        "ollama": ScriptedProvider("ollama", "local", ["local reply"], available=kw.get("local", False)),
        "dev": DevProvider(),
    }


def tool_results(provider):
    """Tool result payloads the model received in its latest request."""
    msgs = provider.requests[-1]["messages"]
    return [json.loads(m.content) for m in msgs if m.role == "tool"]


async def test_plain_conversation(make_core):
    p = providers(["Hello! How can I help?"])
    core = make_core(p)
    res = await core.orchestrator.handle("hi")
    assert res["ok"] and res["text"] == "Hello! How can I help?" and res["provider"] == "anthropic"
    assert not res["is_fallback"]
    req = p["anthropic"].requests[0]
    assert "<context>" not in req["system"]  # system prompt stays constant
    assert "local_time" in req["messages"][0].context
    assert "sonos_set_volume" in req["tools"]


async def test_tool_call_executes_and_result_returns_to_model(make_core, fake_ha):
    p = providers([[("sonos_set_volume", {"speaker": "kitchen", "level": 30})], "Kitchen is at 30%."])
    core = make_core(p)
    res = await core.orchestrator.handle("set kitchen volume to 30")
    assert res["text"] == "Kitchen is at 30%."
    assert fake_ha.state.calls[-1]["service"] == "media_player.volume_set"
    result = tool_results(p["anthropic"])[0]
    assert result["ok"] is True and result["verified"] is True and result["data"]["volume"] == 30
    hist = core.history.list()[0]
    assert hist["status"] == "succeeded" and hist["verified"] is True and hist["decision"] == "allow"


async def test_invalid_arguments_go_back_as_errors(make_core, fake_ha):
    p = providers([[("sonos_set_volume", {"speaker": "kitchen", "level": 150, "cmd": "x"})], "Sorry."])
    core = make_core(p)
    await core.orchestrator.handle("max volume")
    result = tool_results(p["anthropic"])[0]
    assert result["ok"] is False and "Invalid arguments" in result["error"]
    assert fake_ha.state.calls == []


async def test_unknown_tool_rejected(make_core):
    p = providers([[("run_shell", {"command": "format c:"})], "I can't do that."])
    core = make_core(p)
    await core.orchestrator.handle("format my disk")
    assert "Unknown tool" in tool_results(p["anthropic"])[0]["error"]


async def test_high_volume_requires_confirmation_approved(make_core, fake_ha):
    p = providers([[("sonos_set_volume", {"speaker": "kitchen", "level": 90})], "Done."])
    core = make_core(p)
    q = core.bus.subscribe()

    async def approve():
        while True:
            ev = await q.get()
            if ev["type"] == "confirmation.requested":
                assert "90" in ev["data"]["summary"]
                assert core.confirmations.resolve(ev["data"]["id"], True)
                return

    approver = asyncio.create_task(approve())
    res = await core.orchestrator.handle("blast the kitchen")
    await approver
    assert res["ok"] and fake_ha.state.calls[-1]["data"]["volume_level"] == 0.9


async def test_confirmation_denied_blocks_action(make_core, fake_ha):
    p = providers([[("sonos_announce", {"speaker": "kitchen", "message": "Dinner"})], "OK, I won't."])
    core = make_core(p)
    q = core.bus.subscribe()

    async def deny():
        while True:
            ev = await q.get()
            if ev["type"] == "confirmation.requested":
                core.confirmations.resolve(ev["data"]["id"], False)
                return

    t = asyncio.create_task(deny())
    await core.orchestrator.handle("announce dinner")
    await t
    assert fake_ha.state.calls == []
    assert "did not approve" in tool_results(p["anthropic"])[0]["error"]
    assert core.history.list()[0]["status"] == "denied"


async def test_dry_run_smart_home(make_core, fake_ha):
    p = providers([[("sonos_playback", {"speaker": "kitchen", "action": "pause"})], "Simulated."])
    core = make_core(p)
    await core.update_settings({"dry_run_smart_home": True})
    await core.orchestrator.handle("pause kitchen")
    result = tool_results(p["anthropic"])[0]
    assert result["dry_run"] is True and "DRY RUN" in result["note"]
    assert fake_ha.state.calls == []


async def test_computer_dry_run_skips_confirmation(make_core):
    p = providers([[("computer_open_app", {"app": "notepad"})], "Would open Notepad."])
    core = make_core(p)
    res = await core.orchestrator.handle("open notepad")
    result = tool_results(p["anthropic"])[0]
    assert res["ok"] and result["dry_run"] is True and result["data"]["would_run"] == ["notepad.exe"]
    assert core.confirmations.pending() == []


async def test_disallowed_app_rejected(make_core):
    p = providers([[("computer_open_app", {"app": "cmd"})], "Not allowed."])
    core = make_core(p)
    await core.orchestrator.handle("open cmd")
    assert "not an approved application" in tool_results(p["anthropic"])[0]["error"]


async def test_role_restrictions(make_core, fake_ha):
    p = providers([[("sonos_set_volume", {"speaker": "kitchen", "level": 20})], "No."])
    core = make_core(p)
    await core.update_settings({"active_profile": "guest"})
    await core.orchestrator.handle("volume 20 kitchen")
    assert "computer_open_app" not in p["anthropic"].requests[0]["tools"]  # not even offered
    assert "Permission denied" in tool_results(p["anthropic"])[0]["error"]
    assert fake_ha.state.calls == []


async def test_profile_switch_starts_new_conversation(make_core):
    core = make_core(providers(["a", "b"]))
    await core.orchestrator.handle("hello")
    first = core.orchestrator.conversation.id
    await core.update_settings({"active_profile": "kid"})
    assert core.orchestrator.conversation.id != first
    assert core.orchestrator.conversation.messages == []


async def test_falls_back_to_dev_when_cloud_fails(make_core):
    p = providers([ProviderError("Anthropic rate limit reached.", retryable=True)])
    core = make_core(p)
    res = await core.orchestrator.handle("system status")
    assert res["ok"] and res["provider"] == "dev" and res["is_fallback"]
    assert "Dev fallback" in res["text"]


async def test_no_provider_available_reports_error(make_core):
    p = providers([], cloud=False)
    core = make_core(p, ai={"allow_dev_fallback": False})
    res = await core.orchestrator.handle("hello")
    assert not res["ok"] and "No AI provider" in res["error"]
    assert core.orchestrator.status == "error"


async def test_max_tool_rounds(make_core):
    loop_step = [("system_get_time", {})]
    p = providers([loop_step] * 10)
    core = make_core(p, ai={"max_tool_rounds": 3})
    res = await core.orchestrator.handle("loop")
    assert "maximum number of tool steps" in res["text"]
    assert len(p["anthropic"].requests) == 3


async def test_cancel_during_confirmation_keeps_transcript_valid(make_core, fake_ha):
    p = providers([[("sonos_announce", {"speaker": "kitchen", "message": "hi"})]])
    core = make_core(p)
    task = asyncio.create_task(core.orchestrator.handle("announce hi"))
    for _ in range(100):
        await asyncio.sleep(0.01)
        if core.confirmations.pending():
            break
    assert core.confirmations.pending()
    assert core.orchestrator.cancel()
    res = await task
    assert res["cancelled"] and fake_ha.state.calls == []
    msgs = core.orchestrator.conversation.messages
    calls = {c.id for m in msgs if m.role == "assistant" for c in m.tool_calls}
    results = {m.tool_call_id for m in msgs if m.role == "tool"}
    assert calls == results  # every tool call has a result
    assert core.orchestrator.status == "idle"


async def test_input_validation_and_busy(make_core):
    core = make_core(providers(["x"]))
    assert (await core.orchestrator.handle("   "))["error"] == "Empty request."
    assert "too long" in (await core.orchestrator.handle("x" * 5000))["error"]


async def test_conversation_trim_keeps_pairs(make_core):
    p = providers([[("system_get_time", {})], "t1", "r2", "r3", "r4", "r5", "r6"])
    core = make_core(p, retention={"max_conversation_messages": 4})
    for i in range(5):
        await core.orchestrator.handle(f"msg {i}")
    msgs = core.orchestrator.conversation.messages
    assert msgs[0].role == "user"
    calls = {c.id for m in msgs if m.role == "assistant" for c in m.tool_calls}
    assert calls == {m.tool_call_id for m in msgs if m.role == "tool"}


async def test_sensitive_results_not_sent_to_cloud(make_core, monkeypatch):
    from jarvis.computer import windows

    monkeypatch.setattr(windows, "IS_WINDOWS", True)
    monkeypatch.setattr(windows, "_enum_windows", lambda: [windows.WindowInfo(1, "Medical results.pdf", 9)])
    p = providers([[("computer_list_windows", {})], "You have one window open."])
    core = make_core(p)
    await core.orchestrator.handle("which windows are open")
    sent = [m for m in p["anthropic"].requests[-1]["messages"] if m.role == "tool"][0]
    assert "Medical" not in sent.content and "Withheld" in sent.content
    assert core.history.list()[0]["result"] == "[personal data hidden]"


async def test_logs_contain_no_secrets_or_message_text(make_core, caplog):
    import logging

    from conftest import HA_TOKEN

    from jarvis.logging_utils import RedactingFilter

    for h in logging.getLogger().handlers:
        h.addFilter(RedactingFilter())
    p = providers([[("sonos_set_volume", {"speaker": "kitchen", "level": 33})], "ok"])
    core = make_core(p)
    with caplog.at_level(logging.DEBUG):
        await core.refresh_integrations()
        await core.orchestrator.handle("my private request about kitchen volume")
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "Calling Home Assistant service media_player.volume_set" in text
    assert HA_TOKEN not in text
    assert "my private request" not in text
