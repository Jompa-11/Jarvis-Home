"""A small fake of the Home Assistant REST API with three Sonos speakers.

Used by the automated tests (in-process, via ``httpx.ASGITransport``) and for
trying the UI without a real Home Assistant::

    python tests/fake_ha.py            # serves on http://127.0.0.1:8123, token "demo-token"

It implements only what JARVIS uses: /api/, /api/config, /api/states,
/api/template (the Sonos discovery template) and media_player services.
"""

from __future__ import annotations

import copy
import json
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import PlainTextResponse

FULL = 1 | 4 | 8 | 16 | 32 | 512 | 4096 | 16384 | 524288 | 1048576
BASIC = 1 | 4 | 16384  # pause, volume, play — no announce, no play_media

INITIAL_STATES: list[dict[str, Any]] = [
    {"entity_id": "media_player.kitchen", "state": "playing",
     "attributes": {"friendly_name": "Kitchen", "volume_level": 0.25, "is_volume_muted": False,
                    "media_title": "So What", "media_artist": "Miles Davis", "supported_features": FULL}},
    {"entity_id": "media_player.living_room", "state": "paused",
     "attributes": {"friendly_name": "Living Room", "volume_level": 0.4, "is_volume_muted": False, "supported_features": FULL}},
    {"entity_id": "media_player.office", "state": "idle",
     "attributes": {"friendly_name": "Office", "volume_level": 0.1, "supported_features": BASIC}},
    {"entity_id": "media_player.tv", "state": "off", "attributes": {"friendly_name": "TV", "supported_features": 21437}},
    {"entity_id": "sensor.sonos_favorites", "state": "2",
     "attributes": {"friendly_name": "Sonos favorites", "items": {"FV:2/3": "P3 Musik", "FV:2/7": "Morning Jazz"}}},
    {"entity_id": "tts.piper", "state": "unknown", "attributes": {"friendly_name": "Piper"}},
]
SONOS_ENTITIES = [
    {"entity_id": "media_player.kitchen", "area": "Kitchen"},
    {"entity_id": "media_player.living_room", "area": "Living room"},
    {"entity_id": "media_player.office", "area": None},
    {"entity_id": "sensor.sonos_favorites", "area": None},
]


def create_fake_ha(token: str = "test-token") -> FastAPI:
    app = FastAPI()
    app.state.states = {s["entity_id"]: copy.deepcopy(s) for s in INITIAL_STATES}
    app.state.calls: list[dict[str, Any]] = []

    def auth(request: Request) -> None:
        if request.headers.get("authorization") != f"Bearer {token}":
            raise HTTPException(401, "Unauthorized")

    @app.get("/api/")
    async def root(request: Request):
        auth(request)
        return {"message": "API running."}

    @app.get("/api/config")
    async def config(request: Request):
        auth(request)
        return {"version": "2026.10.1", "location_name": "Demo Home"}

    @app.get("/api/states")
    async def states(request: Request):
        auth(request)
        return list(app.state.states.values())

    @app.get("/api/states/{entity_id}")
    async def state(entity_id: str, request: Request):
        auth(request)
        if entity_id not in app.state.states:
            raise HTTPException(404, "Entity not found.")
        return app.state.states[entity_id]

    @app.post("/api/template")
    async def template(request: Request):
        auth(request)
        body = await request.json()
        if "integration_entities('sonos')" in body.get("template", ""):
            return PlainTextResponse(json.dumps(SONOS_ENTITIES))
        raise HTTPException(400, "Template not supported by fake")

    @app.post("/api/services/media_player/{service}")
    async def service(service: str, request: Request):
        auth(request)
        data = await request.json()
        app.state.calls.append({"service": f"media_player.{service}", "data": data})
        st = app.state.states.get(data.get("entity_id"))
        if st is None:
            raise HTTPException(400, "Unknown entity")
        attrs = st["attributes"]
        if service == "media_play":
            st["state"] = "playing"
        elif service == "media_pause":
            st["state"] = "paused"
        elif service == "media_stop":
            st["state"] = "idle"
        elif service == "volume_set":
            attrs["volume_level"] = float(data["volume_level"])
        elif service == "play_media" and not data.get("announce"):
            st["state"] = "playing"
            fav = app.state.states["sensor.sonos_favorites"]["attributes"]["items"].get(data.get("media_content_id"))
            attrs["media_title"] = fav
        return [st]

    return app


if __name__ == "__main__":  # pragma: no cover
    import uvicorn

    uvicorn.run(create_fake_ha("demo-token"), host="127.0.0.1", port=8123, log_level="warning")
