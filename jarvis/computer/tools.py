"""Tools exposed to the model for restricted computer control."""

from __future__ import annotations

from ..core.tools import Risk, ToolContext, ToolRegistry, ToolResult, ToolSpec, schema
from .windows import ComputerError, WindowsControl


def register_computer_tools(registry: ToolRegistry, wc: WindowsControl) -> None:
    async def list_apps(args, ctx: ToolContext) -> ToolResult:
        return ToolResult(ok=True, data={"apps": wc.list_apps()})

    async def open_app(args, ctx: ToolContext) -> ToolResult:
        try:
            return ToolResult(ok=True, data=wc.open_app(args["app"], dry_run=ctx.dry_run), dry_run=ctx.dry_run)
        except (ComputerError, OSError) as exc:
            return ToolResult(ok=False, error=str(exc))

    async def list_windows(args, ctx: ToolContext) -> ToolResult:
        try:
            wins = wc.list_windows()
        except ComputerError as exc:
            return ToolResult(ok=False, error=str(exc))
        return ToolResult(ok=True, data={"windows": [w.title for w in wins][:50]}, sensitivity="personal")

    async def focus_window(args, ctx: ToolContext) -> ToolResult:
        try:
            data = wc.focus_window(args["title_contains"], dry_run=ctx.dry_run)
        except ComputerError as exc:
            return ToolResult(ok=False, error=str(exc))
        verified = data.pop("verified", None)
        return ToolResult(ok=True, data=data, dry_run=ctx.dry_run, verified=verified, sensitivity="personal")

    registry.register(
        ToolSpec(
            name="computer_list_apps",
            description="List the applications JARVIS is allowed to open.",
            parameters=schema({}),
            handler=list_apps,
            risk=Risk.READ,
            category="computer",
        )
    )
    registry.register(
        ToolSpec(
            name="computer_open_app",
            description="Open an approved application by its key from computer_list_apps.",
            parameters=schema({"app": {"type": "string", "pattern": "^[A-Za-z0-9_-]{1,40}$"}}, ["app"]),
            handler=open_app,
            risk=Risk.MEDIUM,
            category="computer",
            supports_dry_run=True,
            confirm_summary=lambda a: f"Open application '{a['app']}'",
        )
    )
    registry.register(
        ToolSpec(
            name="computer_list_windows",
            description="List titles of open windows on the PC (personal data; may be withheld from cloud models).",
            parameters=schema({}),
            handler=list_windows,
            risk=Risk.READ,
            category="computer",
            output_sensitivity="personal",
        )
    )
    registry.register(
        ToolSpec(
            name="computer_focus_window",
            description="Bring an open window to the front, matched by part of its title.",
            parameters=schema({"title_contains": {"type": "string", "minLength": 2, "maxLength": 100}}, ["title_contains"]),
            handler=focus_window,
            risk=Risk.LOW,
            category="computer",
            supports_dry_run=True,
            output_sensitivity="personal",
            confirm_summary=lambda a: f"Switch to window containing '{a['title_contains']}'",
        )
    )
