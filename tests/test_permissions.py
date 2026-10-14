import asyncio

import pytest

from jarvis.core.events import EventBus
from jarvis.core.permissions import ConfirmationManager, Decision, PermissionPolicy
from jarvis.core.tools import Risk, ToolResult, ToolSpec, schema


async def _noop(args, ctx):
    return ToolResult(ok=True)


def spec(risk, category="media", risk_for_args=None):
    return ToolSpec("t_" + risk.name.lower(), "d", schema({}), _noop, risk, category, risk_for_args=risk_for_args)


@pytest.mark.parametrize(
    "role, risk, category, expected",
    [
        ("owner", Risk.READ, "media", Decision.ALLOW),
        ("owner", Risk.LOW, "media", Decision.ALLOW),
        ("owner", Risk.MEDIUM, "computer", Decision.ALLOW),  # ordinary owner actions run straight away
        ("owner", Risk.HIGH, "computer", Decision.CONFIRM),
        ("adult", Risk.HIGH, "computer", Decision.DENY),
        ("child", Risk.LOW, "media", Decision.ALLOW),
        ("child", Risk.MEDIUM, "media", Decision.DENY),
        ("child", Risk.READ, "computer", Decision.DENY),
        ("guest", Risk.READ, "media", Decision.ALLOW),
        ("guest", Risk.LOW, "media", Decision.DENY),
        ("intruder", Risk.READ, "media", Decision.DENY),
    ],
)
def test_policy_matrix(role, risk, category, expected):
    assert PermissionPolicy().evaluate(role, spec(risk, category), {}).decision is expected


def test_argument_dependent_risk_escalates():
    s = spec(Risk.LOW, risk_for_args=lambda a: Risk.HIGH if a.get("level", 0) > 60 else Risk.LOW)
    assert PermissionPolicy().evaluate("owner", s, {"level": 30}).decision is Decision.ALLOW
    assert PermissionPolicy().evaluate("owner", s, {"level": 90}).decision is Decision.CONFIRM
    # An adult still has to confirm a MEDIUM action; only the owner skips that.
    assert PermissionPolicy().evaluate("adult", spec(Risk.MEDIUM, "computer"), {}).decision is Decision.CONFIRM


def test_high_risk_always_confirms_even_for_the_owner():
    assert PermissionPolicy().evaluate("owner", spec(Risk.HIGH, "computer"), {}).decision is Decision.CONFIRM


def test_tools_offered_per_role():
    tools = [spec(Risk.READ, "system"), spec(Risk.MEDIUM, "computer"), spec(Risk.LOW, "media")]
    names = lambda role: {t.name for t in PermissionPolicy().tools_for_role(role, tools)}  # noqa: E731
    assert names("owner") == {"t_read", "t_medium", "t_low"}
    assert names("guest") == {"t_read"}
    assert names("child") == {"t_read", "t_low"}


async def test_confirmation_approve_deny_timeout():
    bus = EventBus()
    q = bus.subscribe()
    cm = ConfirmationManager(bus, timeout_s=5)

    async def approve_when_asked(value):
        ev = await q.get()
        assert ev["type"] == "confirmation.requested"
        assert cm.resolve(ev["data"]["id"], value)

    for value in (True, False):
        task = asyncio.create_task(approve_when_asked(value))
        assert await cm.request(tool="x", summary="s", risk=Risk.MEDIUM, profile_id="p", request_id="r") is value
        await task
        await q.get()  # resolved event

    short = ConfirmationManager(bus, timeout_s=0.05)
    assert await short.request(tool="x", summary="s", risk=Risk.MEDIUM, profile_id="p", request_id="r") is False
    assert short.pending() == []
    assert cm.resolve("does-not-exist", True) is False
