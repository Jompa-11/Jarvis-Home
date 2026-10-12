import pytest
from conftest import ScriptedProvider

from jarvis.config import AIConfig, ModelRoute, PrivacyConfig
from jarvis.providers.base import ChatMessage, ProviderError
from jarvis.providers.router import NoProviderAvailable, ProviderRouter, categorize
from jarvis.providers.usage import UsageTracker


def router(providers, privacy=None, budget=10.0, allow_dev=True, routes=None):
    ai = AIConfig(
        allow_dev_fallback=allow_dev,
        monthly_budget_usd=budget,
        routes=routes or {"conversation": [ModelRoute(provider="cloud"), ModelRoute(provider="local"), ModelRoute(provider="dev")]},
    )
    usage = UsageTracker(None, ai.pricing, ai.monthly_budget_usd)
    return ProviderRouter(providers, ai, privacy or PrivacyConfig(), usage)


def trio(**kw):
    return {
        "cloud": ScriptedProvider("cloud", "cloud", ["from cloud"], available=kw.get("cloud", True)),
        "local": ScriptedProvider("local", "local", ["from local"], available=kw.get("local", True)),
        "dev": ScriptedProvider("dev", "dev", ["from dev"]),
    }


MSGS = [ChatMessage(role="user", content="hi")]


async def complete(r):
    return await r.complete(category="conversation", system="s", messages=MSGS, tools=[])


async def test_primary_provider_used():
    resp, info = await complete(router(trio()))
    assert resp.text == "from cloud" and info.provider == "cloud" and not info.is_fallback


async def test_falls_back_when_unavailable():
    resp, info = await complete(router(trio(cloud=False)))
    assert info.provider == "local" and info.is_fallback
    assert info.attempts[0].provider == "cloud"


async def test_falls_back_on_provider_error():
    p = trio()
    p["cloud"].script = [ProviderError("boom")]
    resp, info = await complete(router(p))
    assert info.provider == "local" and "boom" in info.attempts[0].outcome


async def test_non_fallback_error_stops():
    p = trio()
    p["cloud"].script = [ProviderError("bad request", fallback_ok=False)]
    with pytest.raises(NoProviderAvailable):
        await complete(router(p))


async def test_local_only_never_calls_cloud():
    p = trio()
    _, info = await complete(router(p, privacy=PrivacyConfig(local_only=True)))
    assert info.provider == "local" and p["cloud"].requests == []


async def test_budget_exhausted_skips_cloud():
    p = trio()
    r = router(p, budget=0.0001)
    r.usage._data[r.usage._month()] = {"cost_usd": 1.0, "requests": 1, "by_model": {}}
    _, info = await complete(r)
    assert info.provider == "local" and "budget" in info.attempts[0].outcome


async def test_dev_fallback_can_be_disabled():
    p = trio(cloud=False, local=False)
    with pytest.raises(NoProviderAvailable):
        await complete(router(p, allow_dev=False))
    _, info = await complete(router(trio(cloud=False, local=False)))
    assert info.provider == "dev" and info.is_fallback


async def test_sensitive_tool_output_withheld_from_cloud_only():
    msgs = [
        ChatMessage(role="user", content="what windows are open"),
        ChatMessage(role="tool", content='{"windows": ["Bank statement.pdf"]}', tool_call_id="c1", sensitivity="personal"),
    ]
    p = trio()
    r = router(p)
    await r.complete(category="conversation", system="s", messages=msgs, tools=[])
    sent = p["cloud"].requests[0]["messages"]
    assert "Bank statement" not in sent[1].content and "Withheld" in sent[1].content
    assert msgs[1].content.startswith('{"windows"')  # original untouched

    p2 = trio(cloud=False)
    await router(p2).complete(category="conversation", system="s", messages=msgs, tools=[])
    assert "Bank statement" in p2["local"].requests[0]["messages"][1].content

    p3 = trio()
    await router(p3, privacy=PrivacyConfig(cloud_max_sensitivity="personal")).complete(
        category="conversation", system="s", messages=msgs, tools=[]
    )
    assert "Bank statement" in p3["cloud"].requests[0]["messages"][1].content


def test_categorize():
    assert categorize("Sänk volymen i köket") == "smart_home"
    assert categorize("pause the music") == "smart_home"
    assert categorize("open notepad") == "computer"
    assert categorize("tell me a joke") == "conversation"
