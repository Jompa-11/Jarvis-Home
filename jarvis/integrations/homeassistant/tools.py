"""Tools exposed to the model for Sonos (via Home Assistant)."""

from __future__ import annotations

from typing import Any

from ...config import HomeAssistantConfig
from ...core.tools import Risk, ToolContext, ToolRegistry, ToolResult, ToolSpec, schema
from .client import HAError
from .sonos import SonosError, SonosService

SPEAKER = {"type": "string", "minLength": 1, "maxLength": 80, "description": "Speaker name, room, or entity_id from sonos_list_speakers."}


def _wrap(fn):
    async def handler(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        try:
            data = await fn(args, ctx)
        except (HAError, SonosError) as exc:
            return ToolResult(ok=False, error=str(exc))
        verified = data.pop("verified", None) if isinstance(data, dict) else None
        return ToolResult(ok=True, data=data, dry_run=ctx.dry_run, verified=verified)

    return handler


def register_sonos_tools(registry: ToolRegistry, sonos: SonosService, cfg: HomeAssistantConfig) -> None:
    async def list_speakers(args, ctx):
        speakers = await sonos.discover(force=bool(args.get("refresh")))
        return {"speakers": [s.public() for s in speakers]}

    async def get_playback(args, ctx):
        sp = await sonos.resolve(args["speaker"])
        return (await sonos.refresh(sp.entity_id)).public()

    async def list_favorites(args, ctx):
        favs = await sonos.favorites()
        return {"favorites": sorted(favs.values())}

    async def playback(args, ctx):
        return await sonos.playback(args["speaker"], args["action"], dry_run=ctx.dry_run)

    async def set_volume(args, ctx):
        return await sonos.set_volume(args["speaker"], int(args["level"]), dry_run=ctx.dry_run)

    async def play_favorite(args, ctx):
        return await sonos.play_favorite(args["speaker"], args["favorite"], dry_run=ctx.dry_run)

    async def announce(args, ctx):
        tts = await sonos.find_tts_entity(cfg.tts_entity)
        return await sonos.announce(args["speaker"], args["message"], args.get("volume"), tts, dry_run=ctx.dry_run)

    def loud(args: dict[str, Any]) -> Risk:
        return Risk.MEDIUM if int(args.get("level", 0)) > cfg.max_volume_without_confirmation else Risk.LOW

    specs = [
        ToolSpec(
            name="sonos_list_speakers",
            description="List Sonos speakers known to Home Assistant with room, playback state, volume and supported actions.",
            parameters=schema({"refresh": {"type": "boolean", "description": "Bypass the 30 s cache."}}),
            handler=_wrap(list_speakers),
            risk=Risk.READ,
            category="media",
        ),
        ToolSpec(
            name="sonos_get_playback",
            description="Read the current playback state, track and volume of one Sonos speaker.",
            parameters=schema({"speaker": SPEAKER}, ["speaker"]),
            handler=_wrap(get_playback),
            risk=Risk.READ,
            category="media",
        ),
        ToolSpec(
            name="sonos_list_favorites",
            description="List Sonos favorites (stations/playlists) that can be played.",
            parameters=schema({}),
            handler=_wrap(list_favorites),
            risk=Risk.READ,
            category="media",
        ),
        ToolSpec(
            name="sonos_playback",
            description="Control playback on a Sonos speaker: play/resume, pause, stop, next or previous track.",
            parameters=schema(
                {"speaker": SPEAKER, "action": {"type": "string", "enum": ["play", "pause", "stop", "next", "previous"]}},
                ["speaker", "action"],
            ),
            handler=_wrap(playback),
            risk=Risk.LOW,
            category="media",
            supports_dry_run=True,
            confirm_summary=lambda a: f"{a['action'].title()} on {a['speaker']}",
        ),
        ToolSpec(
            name="sonos_set_volume",
            description="Set the volume (0-100) of a Sonos speaker.",
            parameters=schema(
                {"speaker": SPEAKER, "level": {"type": "integer", "minimum": 0, "maximum": 100}}, ["speaker", "level"]
            ),
            handler=_wrap(set_volume),
            risk=Risk.LOW,
            risk_for_args=loud,
            category="media",
            supports_dry_run=True,
            confirm_summary=lambda a: f"Set {a['speaker']} volume to {a['level']}%",
        ),
        ToolSpec(
            name="sonos_play_favorite",
            description="Play a Sonos favorite (by its title from sonos_list_favorites) on a speaker.",
            parameters=schema(
                {"speaker": SPEAKER, "favorite": {"type": "string", "minLength": 1, "maxLength": 120}}, ["speaker", "favorite"]
            ),
            handler=_wrap(play_favorite),
            risk=Risk.LOW,
            category="media",
            supports_dry_run=True,
            timeout_s=20,
            confirm_summary=lambda a: f"Play '{a['favorite']}' on {a['speaker']}",
        ),
        ToolSpec(
            name="sonos_announce",
            description="Speak a short announcement on a Sonos speaker via Home Assistant text-to-speech (overlays current audio).",
            parameters=schema(
                {
                    "speaker": SPEAKER,
                    "message": {"type": "string", "minLength": 1, "maxLength": 300},
                    "volume": {"type": "integer", "minimum": 0, "maximum": 100},
                },
                ["speaker", "message"],
            ),
            handler=_wrap(announce),
            risk=Risk.MEDIUM,
            category="media",
            supports_dry_run=True,
            timeout_s=20,
            confirm_summary=lambda a: f"Announce on {a['speaker']}: “{a['message']}”",
        ),
    ]
    for spec in specs:
        registry.register(spec)
