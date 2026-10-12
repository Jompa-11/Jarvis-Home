"""Executes model-requested tool calls safely.

Every call goes through the same gate, in this order:

1. Schema validation of the arguments.
2. Permission policy for the active profile's role.
3. User confirmation when the policy requires it.
4. The adapter handler, under a timeout, honouring dry-run mode.

Each step is recorded in the tool history and published to the UI.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import time
import uuid
from collections import deque
from dataclasses import asdict, dataclass, field
from typing import Any

from ..providers.base import ToolCall
from .events import EventBus
from .permissions import ConfirmationManager, Decision, PermissionPolicy
from .tools import Risk, ToolContext, ToolRegistry, ToolResult, ToolValidationError

log = logging.getLogger(__name__)

MAX_RESULT_CHARS = 8000


@dataclass
class ToolExecution:
    id: str
    request_id: str
    tool: str
    arguments: dict[str, Any]
    profile_id: str
    status: str = "pending"  # pending|awaiting_confirmation|running|succeeded|failed|denied|cancelled
    risk: str = "read"
    decision: str = ""
    dry_run: bool = False
    verified: bool | None = None
    summary: str = ""
    error: str | None = None
    result: Any = None
    sensitivity: str = "normal"
    started_at: float = field(default_factory=time.time)
    finished_at: float | None = None

    def public(self) -> dict[str, Any]:
        d = asdict(self)
        if self.sensitivity != "normal":
            # Sensitive results are not echoed into the shared history view.
            d["result"] = f"[{self.sensitivity} data hidden]"
        return d


class ToolHistory:
    def __init__(self, size: int) -> None:
        self._items: deque[ToolExecution] = deque(maxlen=size)

    def add(self, ex: ToolExecution) -> None:
        self._items.append(ex)

    def list(self) -> list[dict[str, Any]]:
        return [e.public() for e in reversed(self._items)]

    def clear(self) -> None:
        self._items.clear()


class ToolExecutor:
    def __init__(
        self,
        registry: ToolRegistry,
        policy: PermissionPolicy,
        confirmations: ConfirmationManager,
        history: ToolHistory,
        bus: EventBus,
    ) -> None:
        self.registry = registry
        self.policy = policy
        self.confirmations = confirmations
        self.history = history
        self.bus = bus

    def _emit(self, ex: ToolExecution) -> None:
        self.bus.publish("tool.updated", ex.public())

    async def execute(
        self, call: ToolCall, ctx: ToolContext, dry_run_categories: frozenset[str] = frozenset()
    ) -> tuple[ToolExecution, ToolResult]:
        ex = ToolExecution(
            id=uuid.uuid4().hex[:12],
            request_id=ctx.request_id,
            tool=call.name,
            arguments=call.arguments if isinstance(call.arguments, dict) else {},
            profile_id=ctx.profile_id,
        )
        self.history.add(ex)

        # 1. Validate
        try:
            spec, args = self.registry.validate(call.name, call.arguments)
        except ToolValidationError as exc:
            return self._finish(ex, ToolResult(ok=False, error=str(exc)), status="failed")

        ex.summary = spec.describe_call(args)
        ex.sensitivity = spec.output_sensitivity

        # 2. Permission policy
        verdict = self.policy.evaluate(ctx.role, spec, args)
        ex.risk = verdict.risk.name.lower()
        ex.decision = verdict.decision.value
        if verdict.decision is Decision.DENY:
            return self._finish(ex, ToolResult(ok=False, error=f"Permission denied: {verdict.reason}"), status="denied")

        will_dry_run = spec.supports_dry_run and spec.category in dry_run_categories
        ctx = dataclasses.replace(ctx, dry_run=will_dry_run)
        # 3. Confirmation (skipped for dry runs, which change nothing)
        if verdict.decision is Decision.CONFIRM and not will_dry_run:
            ex.status = "awaiting_confirmation"
            self._emit(ex)
            approved = await self.confirmations.request(
                tool=spec.name, summary=ex.summary, risk=verdict.risk, profile_id=ctx.profile_id, request_id=ctx.request_id
            )
            if not approved:
                return self._finish(
                    ex, ToolResult(ok=False, error="The user did not approve this action. Do not retry it."), status="denied"
                )

        # 4. Execute
        ex.status = "running"
        self._emit(ex)
        attempts = 2 if spec.risk == Risk.READ else 1  # only reads are retried
        result: ToolResult | None = None
        for attempt in range(attempts):
            try:
                result = await asyncio.wait_for(spec.handler(args, ctx), timeout=spec.timeout_s)
                break
            except asyncio.TimeoutError:
                result = ToolResult(ok=False, error=f"{spec.name} timed out after {spec.timeout_s:.0f}s.")
            except asyncio.CancelledError:
                self._finish(ex, ToolResult(ok=False, error="Cancelled."), status="cancelled")
                raise
            except Exception as exc:  # adapters must not crash the core
                log.exception("Tool %s failed", spec.name)
                result = ToolResult(ok=False, error=f"{spec.name} failed: {type(exc).__name__}: {exc}")
                break
        assert result is not None
        if not result.sensitivity or result.sensitivity == "normal":
            result.sensitivity = spec.output_sensitivity
        return self._finish(ex, result, status="succeeded" if result.ok else "failed")

    def _finish(self, ex: ToolExecution, result: ToolResult, *, status: str) -> tuple[ToolExecution, ToolResult]:
        ex.status = status
        ex.dry_run = result.dry_run
        ex.verified = result.verified
        ex.error = result.error
        ex.result = result.data
        ex.sensitivity = result.sensitivity
        ex.finished_at = time.time()
        self._emit(ex)
        return ex, result


def result_to_model_text(result: ToolResult) -> str:
    text = json.dumps(result.to_model_payload(), ensure_ascii=False, default=str)
    if len(text) > MAX_RESULT_CHARS:
        text = text[:MAX_RESULT_CHARS] + "…[truncated]"
    return text
