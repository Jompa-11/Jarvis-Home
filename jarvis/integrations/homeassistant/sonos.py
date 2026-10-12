"""Sonos control through Home Assistant's Sonos integration.

Speakers are discovered from Home Assistant (never assumed), each speaker's
``supported_features`` bitmask decides which actions are offered, and every
action is verified by reading the state back where that is possible.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote

from .client import HAError, HomeAssistantClient

log = logging.getLogger(__name__)

# homeassistant.components.media_player.MediaPlayerEntityFeature
FEATURES = {
    "pause": 1,
    "volume_set": 4,
    "volume_mute": 8,
    "previous": 16,
    "next": 32,
    "play_media": 512,
    "stop": 4096,
    "play": 16384,
    "grouping": 524288,
    "announce": 1048576,
}

DISCOVERY_TEMPLATE = (
    "{% set ns = namespace(items=[]) %}"
    "{% for e in integration_entities('sonos') %}"
    "{% set ns.items = ns.items + [{'entity_id': e, 'area': area_name(e)}] %}"
    "{% endfor %}"
    "{{ ns.items | tojson }}"
)

PLAYBACK_SERVICES = {
    "play": ("media_play", "play", {"playing"}),
    "pause": ("media_pause", "pause", {"paused", "idle"}),
    "stop": ("media_stop", "stop", {"idle", "paused", "off"}),
    "next": ("media_next_track", "next", None),
    "previous": ("media_previous_track", "previous", None),
}


class SonosError(Exception):
    pass


@dataclass
class Speaker:
    entity_id: str
    name: str
    area: str | None
    state: str
    volume: int | None
    muted: bool | None
    media_title: str | None
    media_artist: str | None
    source: str | None
    supported: list[str]
    group_members: list[str] = field(default_factory=list)

    def supports(self, feature: str) -> bool:
        return feature in self.supported

    def public(self) -> dict[str, Any]:
        return {
            "entity_id": self.entity_id,
            "name": self.name,
            "area": self.area,
            "state": self.state,
            "volume": self.volume,
            "muted": self.muted,
            "media_title": self.media_title,
            "media_artist": self.media_artist,
            "source": self.source,
            "supported_actions": self.supported,
            "group_members": self.group_members,
        }


def speaker_from_state(state: dict[str, Any], area: str | None) -> Speaker:
    attrs = state.get("attributes") or {}
    feats = int(attrs.get("supported_features") or 0)
    vol = attrs.get("volume_level")
    return Speaker(
        entity_id=state["entity_id"],
        name=attrs.get("friendly_name") or state["entity_id"],
        area=area,
        state=state.get("state", "unknown"),
        volume=round(float(vol) * 100) if isinstance(vol, (int, float)) else None,
        muted=attrs.get("is_volume_muted"),
        media_title=attrs.get("media_title"),
        media_artist=attrs.get("media_artist"),
        source=attrs.get("source"),
        supported=[name for name, bit in FEATURES.items() if feats & bit],
        group_members=list(attrs.get("group_members") or []),
    )


class SonosService:
    CACHE_TTL_S = 30.0

    def __init__(self, ha: HomeAssistantClient, *, verify_delay_s: float = 0.5, verify_timeout_s: float = 4.0) -> None:
        self.ha = ha
        self._verify_delay_s = verify_delay_s
        self._verify_timeout_s = verify_timeout_s
        self._cache: tuple[float, list[Speaker], list[str]] | None = None
        self.last_error: str | None = None

    # -- discovery -------------------------------------------------------------
    async def _sonos_entities(self) -> list[dict[str, Any]]:
        rendered = await self.ha.render_template(DISCOVERY_TEMPLATE)
        try:
            items = json.loads(rendered)
        except json.JSONDecodeError as exc:
            raise SonosError("Could not read the Sonos entity list from Home Assistant.") from exc
        return items if isinstance(items, list) else []

    async def discover(self, *, force: bool = False) -> list[Speaker]:
        if self._cache and not force and time.monotonic() - self._cache[0] < self.CACHE_TTL_S:
            return self._cache[1]
        try:
            entities = await self._sonos_entities()
            areas = {e["entity_id"]: e.get("area") for e in entities if isinstance(e, dict) and "entity_id" in e}
            states = await self.ha.get_states()
        except (HAError, SonosError) as exc:
            self.last_error = str(exc)
            raise
        speakers = [
            speaker_from_state(s, areas.get(s["entity_id"]))
            for s in states
            if s.get("entity_id", "").startswith("media_player.") and s["entity_id"] in areas
        ]
        speakers.sort(key=lambda s: (s.area or "~", s.name.lower()))
        sensors = [eid for eid in areas if eid.startswith("sensor.")]
        self._cache = (time.monotonic(), speakers, sensors)
        self.last_error = None
        return speakers

    def cached_speakers(self) -> list[Speaker]:
        return self._cache[1] if self._cache else []

    async def resolve(self, query: str) -> Speaker:
        speakers = await self.discover()
        if not speakers:
            raise SonosError("No Sonos speakers were found in Home Assistant.")
        q = query.strip().lower()
        for s in speakers:
            if s.entity_id.lower() == q:
                return s
        exact = [s for s in speakers if s.name.lower() == q or (s.area or "").lower() == q]
        if len(exact) == 1:
            return exact[0]
        partial = exact or [s for s in speakers if q in s.name.lower() or q in (s.area or "").lower() or q in s.entity_id.lower()]
        if len(partial) == 1:
            return partial[0]
        options = ", ".join(f"{s.name} ({s.area or 'no room'})" for s in (partial or speakers))
        if partial:
            raise SonosError(f"'{query}' matches several speakers: {options}. Which one?")
        raise SonosError(f"No speaker matches '{query}'. Available: {options}.")

    async def refresh(self, entity_id: str) -> Speaker:
        state = await self.ha.get_state(entity_id)
        area = next((s.area for s in self.cached_speakers() if s.entity_id == entity_id), None)
        speaker = speaker_from_state(state, area)
        if self._cache:
            # Keep the cached view (shown in the UI) in step with the device.
            ts, speakers, sensors = self._cache
            self._cache = (ts, [speaker if s.entity_id == entity_id else s for s in speakers], sensors)
        return speaker

    # -- favorites ---------------------------------------------------------------
    async def favorites(self) -> dict[str, str]:
        await self.discover()
        sensors = self._cache[2] if self._cache else []
        for eid in sensors:
            try:
                st = await self.ha.get_state(eid)
            except HAError:
                continue
            items = (st.get("attributes") or {}).get("items")
            if isinstance(items, dict) and items:
                return {str(k): str(v) for k, v in items.items()}
        raise SonosError(
            "No Sonos favorites are available. In Home Assistant, enable the 'Sonos favorites' sensor "
            "(Settings → Devices & services → Sonos → entities)."
        )

    async def resolve_favorite(self, name: str) -> tuple[str, str]:
        favs = await self.favorites()
        q = name.strip().lower()
        exact = [(k, v) for k, v in favs.items() if v.lower() == q]
        matches = exact or [(k, v) for k, v in favs.items() if q in v.lower()]
        if len(matches) == 1:
            return matches[0]
        if not matches:
            raise SonosError(f"No favorite matches '{name}'. Available: {', '.join(sorted(favs.values()))}.")
        raise SonosError(f"'{name}' matches several favorites: {', '.join(v for _, v in matches)}.")

    # -- actions -------------------------------------------------------------------
    async def _wait_for(self, entity_id: str, predicate) -> tuple[bool, Speaker | None]:
        deadline = time.monotonic() + self._verify_timeout_s
        latest: Speaker | None = None
        while True:
            await asyncio.sleep(self._verify_delay_s)
            try:
                latest = await self.refresh(entity_id)
            except HAError:
                latest = None
            if latest is not None and predicate(latest):
                return True, latest
            if time.monotonic() >= deadline:
                return False, latest

    def _require(self, sp: Speaker, feature: str) -> None:
        if not sp.supports(feature):
            raise SonosError(f"{sp.name} does not support '{feature}' (according to Home Assistant).")

    async def playback(self, speaker: str, action: str, *, dry_run: bool) -> dict[str, Any]:
        if action not in PLAYBACK_SERVICES:
            raise SonosError(f"Unknown playback action '{action}'.")
        service, feature, expected = PLAYBACK_SERVICES[action]
        sp = await self.resolve(speaker)
        self._require(sp, feature)
        call = {"service": f"media_player.{service}", "entity_id": sp.entity_id}
        if dry_run:
            return {"speaker": sp.name, "would_call": call, "current_state": sp.state}
        await self.ha.call_service("media_player", service, {"entity_id": sp.entity_id})
        if expected is None:
            await asyncio.sleep(self._verify_delay_s)
            latest = await self.refresh(sp.entity_id)
            return {"speaker": sp.name, "action": action, "now_playing": latest.media_title, "verified": None}
        ok, latest = await self._wait_for(sp.entity_id, lambda s: s.state in expected)
        return {"speaker": sp.name, "action": action, "state": latest.state if latest else None, "verified": ok}

    async def set_volume(self, speaker: str, level: int, *, dry_run: bool) -> dict[str, Any]:
        sp = await self.resolve(speaker)
        self._require(sp, "volume_set")
        call = {"service": "media_player.volume_set", "entity_id": sp.entity_id, "volume_level": level / 100}
        if dry_run:
            return {"speaker": sp.name, "would_call": call, "current_volume": sp.volume}
        await self.ha.call_service("media_player", "volume_set", {"entity_id": sp.entity_id, "volume_level": round(level / 100, 3)})
        ok, latest = await self._wait_for(sp.entity_id, lambda s: s.volume is not None and abs(s.volume - level) <= 2)
        return {"speaker": sp.name, "requested_volume": level, "volume": latest.volume if latest else None, "verified": ok}

    async def play_favorite(self, speaker: str, favorite: str, *, dry_run: bool) -> dict[str, Any]:
        sp = await self.resolve(speaker)
        self._require(sp, "play_media")
        fav_id, fav_title = await self.resolve_favorite(favorite)
        data = {"entity_id": sp.entity_id, "media_content_type": "favorite_item_id", "media_content_id": fav_id}
        if dry_run:
            return {"speaker": sp.name, "favorite": fav_title, "would_call": {"service": "media_player.play_media", **data}}
        await self.ha.call_service("media_player", "play_media", data)
        ok, latest = await self._wait_for(sp.entity_id, lambda s: s.state == "playing")
        return {"speaker": sp.name, "favorite": fav_title, "state": latest.state if latest else None, "verified": ok}

    async def announce(self, speaker: str, message: str, volume: int | None, tts_entity: str, *, dry_run: bool) -> dict[str, Any]:
        sp = await self.resolve(speaker)
        self._require(sp, "announce")
        self._require(sp, "play_media")
        data: dict[str, Any] = {
            "entity_id": sp.entity_id,
            "media_content_type": "music",
            "media_content_id": f"media-source://tts/{tts_entity}?message={quote(message)}",
            "announce": True,
        }
        if volume is not None:
            data["extra"] = {"volume": round(volume / 100, 3)}
        if dry_run:
            return {"speaker": sp.name, "would_call": {"service": "media_player.play_media", **data}}
        await self.ha.call_service("media_player", "play_media", data)
        # Home Assistant gives no reliable signal that an announcement was heard.
        return {"speaker": sp.name, "announced": message, "verified": None}

    async def find_tts_entity(self, configured: str | None) -> str:
        if configured:
            return configured
        states = await self.ha.get_states()
        tts = sorted(s["entity_id"] for s in states if s.get("entity_id", "").startswith("tts."))
        if not tts:
            raise SonosError("No text-to-speech engine found in Home Assistant (add e.g. Piper or Google Translate TTS).")
        return tts[0]
