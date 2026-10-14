"""Local HTTP + WebSocket server for the JARVIS interface.

Security model for a loopback-only control API:

* The server binds to loopback only (enforced by config validation).
* The ``Host`` header must be a loopback name, which blocks DNS-rebinding attacks.
* Every API call needs a per-launch random token. The token is embedded in the
  page this server serves; other websites open in the same browser cannot read
  it (same-origin policy), so they cannot drive the API (no CSRF).
* WebSocket connections must present the token and a same-origin ``Origin``.
"""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import logging
import secrets
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, ValidationError

from .. import diagnostics
from ..app_core import JarvisCore

log = logging.getLogger(__name__)

WEB_DIR = Path(__file__).resolve().parent.parent / "web"


class ChatIn(BaseModel):
    text: str = Field(min_length=1, max_length=4000)


class ConfirmIn(BaseModel):
    approve: bool


class PauseIn(BaseModel):
    paused: bool


class MuteIn(BaseModel):
    minutes: float = Field(ge=0, le=24 * 60)  # 0 = unmute


class AckIn(BaseModel):
    id: str | None = Field(None, max_length=32)  # None = acknowledge all


def create_app(core: JarvisCore, *, token: str | None = None, extra_hosts: set[str] | None = None) -> FastAPI:
    token = token or secrets.token_urlsafe(32)
    port = core.cfg.server.port
    allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}", f"[::1]:{port}", "127.0.0.1", "localhost"} | (extra_hosts or set())

    @contextlib.asynccontextmanager
    async def lifespan(_app: FastAPI):
        await core.start()
        try:
            yield
        finally:
            await core.stop()

    app = FastAPI(title="JARVIS", version="0.1.0", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.token = token
    app.state.core = core

    def token_ok(value: str | None) -> bool:
        return bool(value) and hmac.compare_digest(value, token)

    @app.middleware("http")
    async def guard(request: Request, call_next):
        host = request.headers.get("host", "")
        if host not in allowed_hosts:
            return JSONResponse({"error": "Host not allowed."}, status_code=403)
        path = request.url.path
        if path.startswith("/api/") and path != "/api/health":
            if not token_ok(request.headers.get("x-jarvis-token")):
                return JSONResponse({"error": "Missing or invalid session token."}, status_code=401)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
            "connect-src 'self' ws://127.0.0.1:* ws://localhost:*; frame-ancestors 'none'"
        )
        return response

    # -- page --------------------------------------------------------------------
    index_html = (WEB_DIR / "index.html").read_text(encoding="utf-8")

    @app.get("/", response_class=HTMLResponse)
    async def index() -> HTMLResponse:
        page = index_html.replace("__JARVIS_TOKEN__", token)
        return HTMLResponse(page, headers={"Cache-Control": "no-store"})

    app.mount("/assets", StaticFiles(directory=WEB_DIR / "assets"), name="assets")

    # -- API -----------------------------------------------------------------------
    @app.get("/api/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/api/state")
    async def state() -> dict[str, Any]:
        return await core.snapshot()

    @app.post("/api/chat")
    async def chat(body: ChatIn) -> dict[str, Any]:
        result = await core.orchestrator.handle(body.text)
        if result.get("busy"):
            raise HTTPException(409, result["error"])
        return result

    @app.post("/api/chat/cancel")
    async def cancel() -> dict[str, bool]:
        return {"cancelled": core.orchestrator.cancel()}

    @app.get("/api/conversation")
    async def conversation() -> dict[str, Any]:
        conv = core.orchestrator.conversation
        return {"id": conv.id, "profile_id": conv.profile_id, "messages": conv.transcript()}

    @app.post("/api/conversation/reset")
    async def reset_conversation() -> dict[str, Any]:
        if core.orchestrator.busy:
            raise HTTPException(409, "Cannot reset while a request is running.")
        core.orchestrator.reset_conversation()
        return {"id": core.orchestrator.conversation.id}

    @app.get("/api/tools")
    async def tools() -> list[dict[str, Any]]:
        return [
            {
                "name": t.name,
                "description": t.description,
                "category": t.category,
                "risk": t.risk.name.lower(),
                "supports_dry_run": t.supports_dry_run,
                "output_sensitivity": t.output_sensitivity,
            }
            for t in core.registry.all()
        ]

    @app.get("/api/history")
    async def history() -> list[dict[str, Any]]:
        return core.history.list()

    @app.delete("/api/history")
    async def clear_history() -> dict[str, bool]:
        core.history.clear()
        core.bus.publish("history.cleared", {})
        return {"cleared": True}

    @app.post("/api/confirmations/{confirmation_id}")
    async def confirm(confirmation_id: str, body: ConfirmIn) -> dict[str, bool]:
        if not core.confirmations.resolve(confirmation_id, body.approve):
            raise HTTPException(404, "No such pending confirmation (it may have expired).")
        return {"resolved": True}

    @app.get("/api/settings")
    async def get_settings() -> dict[str, Any]:
        return core.settings.current.model_dump()

    @app.patch("/api/settings")
    async def patch_settings(patch: dict[str, Any]) -> dict[str, Any]:
        try:
            return await core.update_settings(patch)
        except (ValueError, ValidationError) as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.post("/api/integrations/refresh")
    async def refresh() -> dict[str, Any]:
        core.ha.reset()
        await core.refresh_integrations()
        return await core.snapshot()

    @app.get("/api/diagnostics")
    async def diag(refresh: bool = False) -> dict[str, Any]:
        if core.diagnostics is None or refresh:
            envs = {"anthropic_api_key": core.cfg.anthropic.api_key_env, "home_assistant_token": core.cfg.homeassistant.token_env}
            core.diagnostics = await asyncio.to_thread(diagnostics.collect, core.cfg.data_dir, envs)
        return core.diagnostics

    # -- monitoring and notifications -----------------------------------------------
    # Notifications and the summary are personal: only for the owner profile.
    def require_owner() -> None:
        if not core.owner_active():
            raise HTTPException(status_code=403, detail="Only the owner profile can see or change notifications.")

    @app.get("/api/monitoring")
    async def monitoring() -> dict[str, Any]:
        require_owner()
        return core.monitor.snapshot()

    @app.post("/api/monitoring/pause")
    async def monitoring_pause(body: PauseIn) -> dict[str, Any]:
        require_owner()
        await core.update_settings({"monitoring_paused": body.paused})  # persisted like other settings
        return core.monitor.snapshot()

    @app.post("/api/notifications/mute")
    async def notifications_mute(body: MuteIn) -> dict[str, Any]:
        require_owner()
        core.monitor.mute(body.minutes)
        return core.monitor.snapshot()

    @app.post("/api/monitoring/summary")
    async def monitoring_summary() -> dict[str, Any]:
        require_owner()
        await core.monitor.summary_now()  # "Sammanfatta nu"
        return core.monitor.snapshot()

    @app.post("/api/notifications/ack")
    async def notifications_ack(body: AckIn) -> dict[str, Any]:
        require_owner()
        count = core.monitor.notifier.acknowledge(body.id)
        core.bus.publish("monitoring", core.monitoring_view())
        return {"acknowledged": count}

    # -- live events -------------------------------------------------------------
    @app.websocket("/ws")
    async def ws(websocket: WebSocket) -> None:
        origin = websocket.headers.get("origin", "")
        host = websocket.headers.get("host", "")
        same_origin = origin in {f"http://{host}", f"https://{host}"}
        if host not in allowed_hosts or not same_origin or not token_ok(websocket.query_params.get("token")):
            await websocket.close(code=4403)
            return
        await websocket.accept()
        queue = core.bus.subscribe()
        try:
            await websocket.send_json({"type": "state", "data": await core.snapshot()})
            while True:
                event = await queue.get()
                await websocket.send_json(event)
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            core.bus.unsubscribe(queue)

    return app
