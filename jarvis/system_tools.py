"""Read-only system tools available to every profile."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from .core.tools import Risk, ToolRegistry, ToolResult, ToolSpec, schema

if TYPE_CHECKING:
    from .app_core import JarvisCore


def register_system_tools(registry: ToolRegistry, core: "JarvisCore") -> None:
    async def get_time(args, ctx) -> ToolResult:
        return ToolResult(
            ok=True,
            data={"local_time": time.strftime("%Y-%m-%d %H:%M:%S"), "weekday": time.strftime("%A"), "timezone": time.strftime("%Z")},
        )

    async def get_status(args, ctx) -> ToolResult:
        snap = await core.snapshot()
        return ToolResult(
            ok=True,
            data={
                "ai_providers": snap["ai"]["providers"],
                "home_assistant": snap["integrations"]["homeassistant"]["state"],
                "sonos_speakers": len(snap["integrations"]["sonos"]["speakers"]),
                "microphone": snap["voice"]["mic_state"],
                "camera": snap["camera"]["state"],
                "dry_run": {"computer": snap["settings"]["dry_run_computer"], "smart_home": snap["settings"]["dry_run_smart_home"]},
            },
        )

    registry.register(ToolSpec("system_get_time", "Get the current local date and time.", schema({}), get_time, Risk.READ, "system"))
    registry.register(
        ToolSpec(
            "system_get_status",
            "Get JARVIS system status: AI providers, Home Assistant, Sonos, microphone, camera and dry-run modes.",
            schema({}),
            get_status,
            Risk.READ,
            "system",
        )
    )
