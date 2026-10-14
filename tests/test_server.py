import pytest
from conftest import ScriptedProvider
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from jarvis.providers.dev_provider import DevProvider
from jarvis.server.app import create_app

TOKEN = "t" * 40


@pytest.fixture
def client(make_core):
    providers = {
        "anthropic": ScriptedProvider("anthropic", "cloud", [], available=False),
        "ollama": ScriptedProvider("ollama", "local", [], available=False),
        "dev": DevProvider(),
    }
    core = make_core(providers)
    app = create_app(core, token=TOKEN, extra_hosts={"testserver"})
    with TestClient(app) as c:
        c.core = core
        yield c


H = {"X-Jarvis-Token": TOKEN}


def test_index_embeds_token_and_security_headers(client):
    r = client.get("/")
    assert r.status_code == 200 and TOKEN in r.text
    assert "frame-ancestors 'none'" in r.headers["content-security-policy"]
    assert r.headers["x-frame-options"] == "DENY"


def test_api_requires_token(client):
    assert client.get("/api/health").status_code == 200
    assert client.get("/api/state").status_code == 401
    assert client.get("/api/state", headers={"X-Jarvis-Token": "wrong"}).status_code == 401
    assert client.post("/api/chat", json={"text": "hi"}).status_code == 401
    assert client.get("/api/state", headers=H).status_code == 200


def test_foreign_host_rejected(client):
    r = client.get("/api/state", headers={**H, "Host": "attacker.example"})
    assert r.status_code == 403


def test_state_reflects_reality(client):
    s = client.get("/api/state", headers=H).json()
    assert s["integrations"]["homeassistant"]["state"] == "connected"
    assert len(s["integrations"]["sonos"]["speakers"]) == 3
    assert s["voice"]["mic_state"] == "not_implemented"
    assert s["camera"]["state"] == "not_implemented"
    assert s["ai"]["status"] == "idle"
    assert {p["name"]: p["available"] for p in s["ai"]["providers"]} == {"anthropic": False, "ollama": False, "dev": True}


def test_chat_with_dev_fallback_runs_real_tools(client):
    r = client.post("/api/chat", json={"text": "list speakers"}, headers=H).json()
    assert r["ok"] and r["provider"] == "dev" and r["is_fallback"]
    assert "Kitchen" in r["text"] and "3 speaker" in r["text"]
    hist = client.get("/api/history", headers=H).json()
    assert hist[0]["tool"] == "sonos_list_speakers" and hist[0]["status"] == "succeeded"
    conv = client.get("/api/conversation", headers=H).json()
    assert conv["messages"][0] == {"role": "user", "text": "list speakers"}


def test_chat_validation(client):
    assert client.post("/api/chat", json={"text": ""}, headers=H).status_code == 422
    assert client.post("/api/chat", json={"text": "x" * 4001}, headers=H).status_code == 422


def test_settings_patch_and_validation(client):
    r = client.patch("/api/settings", json={"dry_run_smart_home": True}, headers=H)
    assert r.status_code == 200 and r.json()["dry_run_smart_home"] is True
    assert client.patch("/api/settings", json={"unknown": 1}, headers=H).status_code == 422
    assert client.patch("/api/settings", json={"active_profile": "nobody"}, headers=H).status_code == 422
    assert client.patch("/api/settings", json={"cloud_max_sensitivity": "everything"}, headers=H).status_code == 422


def test_confirmation_unknown_id(client):
    assert client.post("/api/confirmations/nope", json={"approve": True}, headers=H).status_code == 404


def test_websocket_requires_token_and_origin(client):
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/ws", headers={"origin": "http://testserver"}) as ws:
            ws.receive_json()
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(f"/ws?token={TOKEN}", headers={"origin": "http://evil.example"}) as ws:
            ws.receive_json()
    with client.websocket_connect(f"/ws?token={TOKEN}", headers={"origin": "http://testserver"}) as ws:
        first = ws.receive_json()
        assert first["type"] == "state" and first["data"]["core"]["status"] == "online"


def test_tools_listing(client):
    tools = {t["name"]: t for t in client.get("/api/tools", headers=H).json()}
    assert tools["computer_list_windows"]["output_sensitivity"] == "personal"
    assert tools["sonos_announce"]["risk"] == "medium"
    # The owner asked for an advanced mode. It exists, but only HIGH risk, confirmed
    # every time, and refused once the turn has read anything from outside.
    shells = [n for n in tools if "shell" in n or "exec" in n]
    assert shells == ["shell_run_powershell"]
    assert tools["shell_run_powershell"]["risk"] == "high"
