import json

import anthropic
import httpx
import httpx2
import pytest

from jarvis.config import AnthropicConfig, OllamaConfig
from jarvis.core.tools import Risk, ToolResult, ToolSpec, schema
from jarvis.providers.anthropic_provider import AnthropicProvider, to_anthropic_messages
from jarvis.providers.base import ChatMessage, ProviderError, ToolCall
from jarvis.providers.dev_provider import DevProvider, match_intent
from jarvis.providers.ollama_provider import OllamaProvider


async def _noop(args, ctx):
    return ToolResult(ok=True)


TOOL = ToolSpec("sonos_set_volume", "Set volume", schema({"level": {"type": "integer"}}, ["level"]), _noop, Risk.LOW)

CLAUDE_RESPONSE = {
    "id": "msg_1",
    "type": "message",
    "role": "assistant",
    "model": "claude-opus-5-5",
    "content": [
        {"type": "thinking", "thinking": "", "signature": "sig-abc"},
        {"type": "text", "text": "Setting volume."},
        {"type": "tool_use", "id": "toolu_1", "name": "sonos_set_volume", "input": {"level": 30}},
    ],
    "stop_reason": "tool_use",
    "stop_sequence": None,
    "usage": {"input_tokens": 120, "output_tokens": 40, "cache_read_input_tokens": 1000, "cache_creation_input_tokens": 0},
}


def anthropic_with_transport(handler):
    http_client = anthropic.DefaultAsyncHttpxClient(transport=httpx2.MockTransport(handler))
    client = anthropic.AsyncAnthropic(api_key="sk-ant-test-key-000000", base_url="https://api.anthropic.test", http_client=http_client, max_retries=0)
    return AnthropicProvider(AnthropicConfig(), client=client)


async def test_anthropic_wire_request_and_parsing():
    seen = {}

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen["url"] = str(request.url)
        seen["headers"] = dict(request.headers)
        seen["body"] = json.loads(request.content)
        return httpx2.Response(200, json=CLAUDE_RESPONSE)

    provider = anthropic_with_transport(handler)
    msgs = [ChatMessage(role="user", content="volume 30", context="local_time: now")]
    resp = await provider.complete(system="SYS", messages=msgs, tools=[TOOL], model="claude-opus-5-5", effort="low", timeout_s=10)

    body = seen["body"]
    assert seen["url"].endswith("/v1/messages?beta=true") or seen["url"].endswith("/v1/messages")
    assert body["model"] == "claude-opus-5-5" and body["system"] == "SYS"
    assert body["output_config"] == {"effort": "low"}
    assert body["fallbacks"] == "default"
    assert "server-side-fallback-2026-07-01" in seen["headers"].get("anthropic-beta", "")
    assert body["cache_control"] == {"type": "ephemeral"}
    assert body["tools"][0]["name"] == "sonos_set_volume" and "input_schema" in body["tools"][0]
    assert "thinking" not in body and "temperature" not in body
    assert body["messages"][0]["content"].startswith("<context>")

    assert resp.text == "Setting volume."
    assert resp.tool_calls == [ToolCall(id="toolu_1", name="sonos_set_volume", arguments={"level": 30})]
    assert resp.usage.cache_read_tokens == 1000
    assert resp.raw_assistant[0]["type"] == "thinking" and resp.raw_assistant[0]["signature"] == "sig-abc"


async def test_anthropic_no_fallback_param_for_haiku():
    seen = {}

    def handler(request):
        seen["body"] = json.loads(request.content)
        seen["beta"] = request.headers.get("anthropic-beta", "")
        return httpx2.Response(200, json={**CLAUDE_RESPONSE, "model": "claude-haiku-5-5"})

    provider = anthropic_with_transport(handler)
    await provider.complete(system="S", messages=[ChatMessage(role="user", content="x")], tools=[], model="claude-haiku-5-5", effort=None, timeout_s=10)
    assert "fallbacks" not in seen["body"] and "server-side-fallback" not in seen["beta"]


@pytest.mark.parametrize(
    "status, retryable, fragment",
    [(401, False, "API key"), (429, True, "rate limit"), (404, False, "not found"), (529, True, "529"), (400, False, "rejected")],
)
async def test_anthropic_error_mapping(status, retryable, fragment):
    def handler(request):
        return httpx2.Response(status, json={"type": "error", "error": {"type": "x", "message": "nope"}})

    provider = anthropic_with_transport(handler)
    with pytest.raises(ProviderError) as exc:
        await provider.complete(system="S", messages=[ChatMessage(role="user", content="x")], tools=[], model=None, effort=None, timeout_s=10)
    assert exc.value.retryable is retryable and fragment.lower() in str(exc.value).lower()


async def test_anthropic_refusal_and_health(monkeypatch):
    def handler(request):
        return httpx2.Response(200, json={**CLAUDE_RESPONSE, "content": [], "stop_reason": "refusal"})

    resp = await anthropic_with_transport(handler).complete(
        system="S", messages=[ChatMessage(role="user", content="x")], tools=[TOOL], model=None, effort=None, timeout_s=10
    )
    assert resp.tool_calls == [] and "can't help" in resp.text

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    h = await AnthropicProvider(AnthropicConfig()).health()
    assert not h.available and "ANTHROPIC_API_KEY" in h.detail
    with pytest.raises(ProviderError, match="No API key"):
        await AnthropicProvider(AnthropicConfig()).complete(system="", messages=[], tools=[], model=None, effort=None, timeout_s=1)


def test_anthropic_message_translation():
    raw = [{"type": "thinking", "thinking": "", "signature": "s"}, {"type": "tool_use", "id": "t1", "name": "a", "input": {}}]
    msgs = [
        ChatMessage(role="user", content="q"),
        ChatMessage(role="assistant", content="", tool_calls=[ToolCall("t1", "a", {}), ToolCall("t2", "b", {})], raw={"anthropic": raw}),
        ChatMessage(role="tool", content='{"ok":true}', tool_call_id="t1"),
        ChatMessage(role="tool", content='{"ok":false}', tool_call_id="t2", is_error=True),
        ChatMessage(role="assistant", content="done (from ollama)", tool_calls=[]),
        ChatMessage(role="user", content="next"),
    ]
    out = to_anthropic_messages(msgs)
    assert [m["role"] for m in out] == ["user", "assistant", "user", "assistant", "user"]
    assert out[1]["content"] is raw  # replayed unchanged (thinking preserved)
    assert [b["tool_use_id"] for b in out[2]["content"]] == ["t1", "t2"]
    assert out[2]["content"][1]["is_error"] is True
    assert out[3]["content"] == [{"type": "text", "text": "done (from ollama)"}]


async def test_ollama_health_and_chat():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/tags":
            return httpx.Response(200, json={"models": [{"name": "qwen2.5:3b"}]})
        body = json.loads(request.content)
        assert body["messages"][0]["role"] == "system" and body["tools"][0]["function"]["name"] == "sonos_set_volume"
        return httpx.Response(200, json={
            "message": {"role": "assistant", "content": "", "tool_calls": [{"function": {"name": "sonos_set_volume", "arguments": {"level": 20}}}]},
            "done_reason": "stop", "prompt_eval_count": 50, "eval_count": 8,
        })

    p = OllamaProvider(OllamaConfig(), transport=httpx.MockTransport(handler))
    assert (await p.health()).available
    resp = await p.complete(system="S", messages=[ChatMessage(role="user", content="vol 20")], tools=[TOOL], model=None, effort=None, timeout_s=5)
    assert resp.tool_calls[0].name == "sonos_set_volume" and resp.tool_calls[0].arguments == {"level": 20}
    assert resp.tool_calls[0].id.startswith("call_")


async def test_ollama_missing_model_and_offline():
    p = OllamaProvider(OllamaConfig(model="llama3.2"), transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"models": [{"name": "qwen2.5:3b"}]})))
    h = await p.health()
    assert not h.available and "ollama pull llama3.2" in h.detail

    def offline(request):
        raise httpx.ConnectError("refused")

    p2 = OllamaProvider(OllamaConfig(), transport=httpx.MockTransport(offline))
    assert not (await p2.health()).available
    with pytest.raises(ProviderError, match="not reachable"):
        await p2.complete(system="S", messages=[], tools=[], model=None, effort=None, timeout_s=1)


@pytest.mark.parametrize(
    "text, tool, args",
    [
        ("list speakers", "sonos_list_speakers", {}),
        ("Vilka högtalare finns?", "sonos_list_speakers", {}),
        ("volume 30 in kitchen", "sonos_set_volume", {"speaker": "kitchen", "level": 30}),
        ("sätt volym 15 i köket", "sonos_set_volume", {"speaker": "köket", "level": 15}),
        ("pause in the living room", "sonos_playback", {"speaker": "living room", "action": "pause"}),
        ("open notepad", "computer_open_app", {"app": "notepad"}),
        ("öppna calculator", "computer_open_app", {"app": "calculator"}),
        ("system status", "system_get_status", {}),
        ("vad är klockan?", "system_get_time", {}),
    ],
)
def test_dev_intents(text, tool, args):
    available = {"sonos_list_speakers", "sonos_set_volume", "sonos_playback", "computer_open_app", "system_get_status", "system_get_time"}
    call = match_intent(text, available)
    assert call is not None and call.name == tool and call.arguments == args


async def test_dev_provider_labels_output():
    resp = await DevProvider().complete(system="", messages=[ChatMessage(role="user", content="tell me a story")], tools=[], model=None, effort=None, timeout_s=1)
    assert resp.text.startswith("[Dev fallback") and resp.tool_calls == []


async def test_dev_help_lists_only_available_commands():
    from jarvis.core.tools import schema as _schema

    def spec(name):
        return ToolSpec(name, "d", _schema({}), _noop)

    tools = [spec("system_get_status"), spec("sonos_list_speakers")]
    resp = await DevProvider().complete(system="", messages=[ChatMessage(role="user", content="hello")], tools=tools, model=None, effort=None, timeout_s=1)
    assert '"system status"' in resp.text and '"list speakers"' in resp.text
    assert "notepad" not in resp.text


def test_dev_status_summary_is_readable():
    import json as _json

    from jarvis.providers.dev_provider import _summarize

    payload = {"ok": True, "data": {
        "ai_providers": [{"name": "anthropic", "available": False, "detail": "Not configured"}, {"name": "dev", "available": True}],
        "home_assistant": "not_configured", "sonos_speakers": 0, "microphone": "not_implemented",
        "camera": "not_implemented", "dry_run": {"computer": True, "smart_home": True},
    }}
    text = _summarize("system_get_status", _json.dumps(payload), False)
    assert "{" not in text and "Home Assistant: not configured" in text and "computer on, smart home on" in text
