"""Tool definitions, registry and argument validation.

A *tool* is the only way the AI can affect anything. Each tool declares:

* a JSON schema for its arguments (validated before execution),
* a risk level (drives the permission policy),
* the sensitivity of its output (drives what may be sent to cloud models),
* whether it honours dry-run mode,
* an async handler implemented by an adapter (Home Assistant, Windows, ...).

Model output is only ever turned into a call to one of these registered
handlers. There is no generic "run this command" tool.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any, Awaitable, Callable

import jsonschema

from ..config import Sensitivity

TOOL_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


class Risk(IntEnum):
    READ = 0  # Reads state. No side effects.
    LOW = 1  # Reversible, low-impact action (pause music, set volume).
    MEDIUM = 2  # Consequential or noticeable by others (announcements, opening apps).
    HIGH = 3  # Destructive or hard to undo. Always confirmed; owner only.


@dataclass
class ToolContext:
    profile_id: str
    role: str
    request_id: str
    # Names of tools in THIS turn whose output was written by someone else.
    # Shared and mutated by the orchestrator as the turn runs.
    tainted_by: set[str] = field(default_factory=set)
    # Set by the executor: True when this call must be simulated, not performed.
    dry_run: bool = False


@dataclass
class ToolResult:
    ok: bool
    data: Any = None
    error: str | None = None
    # True when the action was simulated rather than performed.
    dry_run: bool = False
    # True/False when the adapter could check the effect; None if it could not.
    verified: bool | None = None
    sensitivity: Sensitivity = "normal"

    def to_model_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"ok": self.ok}
        if self.data is not None:
            payload["data"] = self.data
        if self.error:
            payload["error"] = self.error
        if self.dry_run:
            payload["dry_run"] = True
            payload["note"] = "DRY RUN: nothing was actually changed."
        if self.verified is not None:
            payload["verified"] = self.verified
        return payload


Handler = Callable[[dict[str, Any], ToolContext], Awaitable[ToolResult]]


@dataclass
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]
    handler: Handler
    risk: Risk = Risk.READ
    category: str = "system"
    output_sensitivity: Sensitivity = "normal"
    supports_dry_run: bool = False
    timeout_s: float = 15.0
    # Optional hook that can raise the risk for specific arguments,
    # e.g. a volume above the configured comfort limit.
    risk_for_args: Callable[[dict[str, Any]], Risk] | None = None
    confirm_summary: Callable[[dict[str, Any]], str] | None = None
    tags: list[str] = field(default_factory=list)
    # Output may contain text written by someone else (e-mail, files, clipboard,
    # window titles). Reading it taints the turn for blocked_after_untrusted tools.
    untrusted_output: bool = False
    # Refused for the rest of a turn in which an untrusted_output tool has run,
    # so text from a message can never reach a command. Enforced by the executor.
    blocked_after_untrusted: bool = False
    # Which dry-run switch applies, when it is not the tool's own category
    # (e.g. files_open_folder is 'personal' but follows the computer dry-run).
    dry_run_category: str | None = None

    def __post_init__(self) -> None:
        if not TOOL_NAME_RE.match(self.name):
            raise ValueError(f"invalid tool name {self.name!r}")
        if self.parameters.get("type") != "object":
            raise ValueError(f"tool {self.name}: parameters schema must be an object")
        jsonschema.Draft202012Validator.check_schema(self.parameters)

    def effective_risk(self, args: dict[str, Any]) -> Risk:
        if self.risk_for_args is None:
            return self.risk
        return max(self.risk, self.risk_for_args(args))

    def describe_call(self, args: dict[str, Any]) -> str:
        if self.confirm_summary is not None:
            try:
                return self.confirm_summary(args)
            except Exception:
                pass
        return f"{self.name}({', '.join(f'{k}={v!r}' for k, v in args.items())})"


class ToolValidationError(Exception):
    pass


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, ToolSpec] = {}

    def register(self, spec: ToolSpec) -> None:
        if spec.name in self._tools:
            raise ValueError(f"tool {spec.name} already registered")
        self._tools[spec.name] = spec

    def get(self, name: str) -> ToolSpec | None:
        return self._tools.get(name)

    def all(self) -> list[ToolSpec]:
        # Sorted so the tool list sent to models is deterministic (prompt cache friendly).
        return [self._tools[k] for k in sorted(self._tools)]

    def validate(self, name: str, args: Any) -> tuple[ToolSpec, dict[str, Any]]:
        spec = self._tools.get(name)
        if spec is None:
            raise ToolValidationError(f"Unknown tool '{name}'.")
        if not isinstance(args, dict):
            raise ToolValidationError("Tool arguments must be a JSON object.")
        validator = jsonschema.Draft202012Validator(spec.parameters)
        errors = sorted(validator.iter_errors(args), key=lambda e: list(e.path))
        if errors:
            details = "; ".join(
                f"{'/'.join(str(p) for p in e.path) or '(arguments)'}: {e.message}" for e in errors[:5]
            )
            raise ToolValidationError(f"Invalid arguments for {name}: {details}")
        return spec, args


def schema(properties: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    """Helper for a closed object schema (no unexpected arguments)."""
    return {
        "type": "object",
        "properties": properties,
        "required": required or [],
        "additionalProperties": False,
    }
