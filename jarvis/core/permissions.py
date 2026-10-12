"""Permission policy and user confirmation.

The policy is deliberately simple and deny-by-default:

* Each role has a set of allowed tool categories and a maximum risk level.
* Within that, actions at or below ``auto_approve_max`` run without asking.
* Anything above requires an explicit confirmation from the UI.
* HIGH risk always requires confirmation and is limited to the owner role.

Confirmations can only be granted through the local API by a person.
The model has no tool that can approve anything.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from .events import EventBus
from .tools import Risk, ToolSpec


class Decision(str, Enum):
    ALLOW = "allow"
    CONFIRM = "confirm"
    DENY = "deny"


@dataclass(frozen=True)
class RolePolicy:
    categories: frozenset[str]
    max_risk: Risk
    auto_approve_max: Risk


ALL_CATEGORIES = frozenset({"system", "smart_home", "media", "computer"})

ROLE_POLICIES: dict[str, RolePolicy] = {
    "owner": RolePolicy(ALL_CATEGORIES, Risk.HIGH, Risk.LOW),
    "adult": RolePolicy(frozenset({"system", "smart_home", "media", "computer"}), Risk.MEDIUM, Risk.LOW),
    "child": RolePolicy(frozenset({"system", "media"}), Risk.LOW, Risk.LOW),
    "guest": RolePolicy(frozenset({"system", "media"}), Risk.READ, Risk.READ),
}


@dataclass
class PolicyResult:
    decision: Decision
    reason: str
    risk: Risk


class PermissionPolicy:
    def __init__(self, role_policies: dict[str, RolePolicy] | None = None) -> None:
        self._roles = role_policies or ROLE_POLICIES

    def tools_for_role(self, role: str, tools: list[ToolSpec]) -> list[ToolSpec]:
        """Tools a role may use at all. Only these are offered to the model."""
        policy = self._roles.get(role)
        if policy is None:
            return []
        return [t for t in tools if t.category in policy.categories and t.risk <= policy.max_risk]

    def evaluate(self, role: str, spec: ToolSpec, args: dict[str, Any]) -> PolicyResult:
        policy = self._roles.get(role)
        risk = spec.effective_risk(args)
        if policy is None:
            return PolicyResult(Decision.DENY, f"Unknown role '{role}'.", risk)
        if spec.category not in policy.categories:
            return PolicyResult(Decision.DENY, f"Role '{role}' may not use {spec.category} tools.", risk)
        if risk > policy.max_risk:
            return PolicyResult(
                Decision.DENY, f"Role '{role}' may not perform {risk.name.lower()}-risk actions.", risk
            )
        if risk >= Risk.HIGH or risk > policy.auto_approve_max:
            return PolicyResult(Decision.CONFIRM, f"{risk.name.title()}-risk action requires confirmation.", risk)
        return PolicyResult(Decision.ALLOW, "Allowed by policy.", risk)


@dataclass
class PendingConfirmation:
    id: str
    tool: str
    summary: str
    risk: str
    profile_id: str
    request_id: str
    created_at: float
    expires_at: float
    future: asyncio.Future[bool] = field(repr=False)

    def public(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "tool": self.tool,
            "summary": self.summary,
            "risk": self.risk,
            "profile_id": self.profile_id,
            "request_id": self.request_id,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
        }


class ConfirmationManager:
    def __init__(self, bus: EventBus, timeout_s: float = 120.0) -> None:
        self._bus = bus
        self._timeout_s = timeout_s
        self._pending: dict[str, PendingConfirmation] = {}

    def pending(self) -> list[dict[str, Any]]:
        return [p.public() for p in self._pending.values()]

    async def request(self, *, tool: str, summary: str, risk: Risk, profile_id: str, request_id: str) -> bool:
        """Ask the user. Returns True only on explicit approval; timeout means no."""
        loop = asyncio.get_running_loop()
        now = time.time()
        pc = PendingConfirmation(
            id=uuid.uuid4().hex[:12],
            tool=tool,
            summary=summary,
            risk=risk.name.lower(),
            profile_id=profile_id,
            request_id=request_id,
            created_at=now,
            expires_at=now + self._timeout_s,
            future=loop.create_future(),
        )
        self._pending[pc.id] = pc
        self._bus.publish("confirmation.requested", pc.public())
        outcome = "denied"
        try:
            approved = await asyncio.wait_for(asyncio.shield(pc.future), timeout=self._timeout_s)
            outcome = "approved" if approved else "denied"
            return approved
        except asyncio.TimeoutError:
            outcome = "expired"
            return False
        except asyncio.CancelledError:
            outcome = "cancelled"
            raise
        finally:
            self._pending.pop(pc.id, None)
            if not pc.future.done():
                pc.future.cancel()
            self._bus.publish("confirmation.resolved", {"id": pc.id, "outcome": outcome})

    def resolve(self, confirmation_id: str, approved: bool) -> bool:
        pc = self._pending.get(confirmation_id)
        if pc is None or pc.future.done():
            return False
        pc.future.set_result(bool(approved))
        return True

    def cancel_for_request(self, request_id: str) -> None:
        for pc in list(self._pending.values()):
            if pc.request_id == request_id and not pc.future.done():
                pc.future.set_result(False)
