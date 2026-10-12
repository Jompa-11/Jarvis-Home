import httpx
import pytest
from conftest import HA_TOKEN

from jarvis.config import HomeAssistantConfig
from jarvis.integrations.homeassistant.client import HAError, HomeAssistantClient
from jarvis.integrations.homeassistant.sonos import SonosError, SonosService


def client(fake_ha, monkeypatch, token=HA_TOKEN, url="http://ha.test", transport=None):
    monkeypatch.setenv("HA_TOKEN", token)
    return HomeAssistantClient(HomeAssistantConfig(url=url, timeout_s=2), transport=transport or httpx.ASGITransport(app=fake_ha))


async def test_health_connected(fake_ha, monkeypatch):
    st = await client(fake_ha, monkeypatch).check()
    assert st.state == "connected" and st.version == "2026.10.1" and st.location_name == "Demo Home"


async def test_health_bad_token(fake_ha, monkeypatch):
    st = await client(fake_ha, monkeypatch, token="wrong-token-123456").check()
    assert st.state == "auth_failed"
    assert "wrong-token" not in st.detail


async def test_health_unreachable(fake_ha, monkeypatch):
    def boom(request):
        raise httpx.ConnectError("refused")

    st = await client(fake_ha, monkeypatch, transport=httpx.MockTransport(boom)).check()
    assert st.state == "unreachable"


async def test_health_timeout(fake_ha, monkeypatch):
    def slow(request):
        raise httpx.ReadTimeout("slow")

    st = await client(fake_ha, monkeypatch, transport=httpx.MockTransport(slow)).check()
    assert st.state == "unreachable" and "in time" in st.detail


async def test_not_configured(monkeypatch):
    monkeypatch.delenv("HA_TOKEN", raising=False)
    c = HomeAssistantClient(HomeAssistantConfig(url="http://ha.test"))
    assert (await c.check()).state == "not_configured"
    with pytest.raises(HAError, match="not configured"):
        await c.get_states()


async def test_service_allowlist_enforced(fake_ha, monkeypatch):
    c = client(fake_ha, monkeypatch)
    with pytest.raises(HAError, match="allowlist"):
        await c.call_service("lock", "unlock", {"entity_id": "lock.front_door"})
    assert fake_ha.state.calls == []


async def test_sonos_discovery_uses_integration_entities(fake_ha, monkeypatch):
    sonos = SonosService(client(fake_ha, monkeypatch))
    speakers = await sonos.discover()
    ids = [s.entity_id for s in speakers]
    assert ids == ["media_player.kitchen", "media_player.living_room", "media_player.office"]
    assert "media_player.tv" not in ids  # not a Sonos entity
    kitchen = speakers[0]
    assert kitchen.area == "Kitchen" and kitchen.volume == 25 and kitchen.supports("announce")
    office = speakers[2]
    assert not office.supports("announce") and not office.supports("play_media") and office.supports("volume_set")


async def test_resolve_speaker(fake_ha, monkeypatch):
    sonos = SonosService(client(fake_ha, monkeypatch))
    assert (await sonos.resolve("kitchen")).entity_id == "media_player.kitchen"
    assert (await sonos.resolve("living room")).entity_id == "media_player.living_room"
    assert (await sonos.resolve("media_player.office")).name == "Office"
    with pytest.raises(SonosError, match="No speaker matches"):
        await sonos.resolve("garage")
    with pytest.raises(SonosError, match="several"):
        await sonos.resolve("i")  # matches kitchen, living room, office


async def test_volume_is_set_and_verified(fake_ha, monkeypatch):
    sonos = SonosService(client(fake_ha, monkeypatch), verify_delay_s=0.01, verify_timeout_s=0.2)
    out = await sonos.set_volume("Kitchen", 35, dry_run=False)
    assert out["verified"] is True and out["volume"] == 35
    assert sonos.cached_speakers()[0].volume == 35  # UI view follows the device
    assert fake_ha.state.calls == [{"service": "media_player.volume_set", "data": {"entity_id": "media_player.kitchen", "volume_level": 0.35}}]


async def test_dry_run_makes_no_service_call(fake_ha, monkeypatch):
    sonos = SonosService(client(fake_ha, monkeypatch))
    out = await sonos.playback("Kitchen", "pause", dry_run=True)
    assert out["would_call"]["service"] == "media_player.media_pause"
    assert fake_ha.state.calls == []


async def test_unsupported_feature_refused(fake_ha, monkeypatch):
    sonos = SonosService(client(fake_ha, monkeypatch))
    with pytest.raises(SonosError, match="does not support 'announce'"):
        await sonos.announce("Office", "Dinner!", None, "tts.piper", dry_run=False)
    assert fake_ha.state.calls == []


async def test_favorites_and_play(fake_ha, monkeypatch):
    sonos = SonosService(client(fake_ha, monkeypatch), verify_delay_s=0.01, verify_timeout_s=0.2)
    favs = await sonos.favorites()
    assert set(favs.values()) == {"P3 Musik", "Morning Jazz"}
    out = await sonos.play_favorite("living room", "jazz", dry_run=False)
    assert out["favorite"] == "Morning Jazz" and out["verified"] is True
    call = fake_ha.state.calls[-1]["data"]
    assert call["media_content_type"] == "favorite_item_id" and call["media_content_id"] == "FV:2/7"


async def test_announce_payload(fake_ha, monkeypatch):
    sonos = SonosService(client(fake_ha, monkeypatch))
    assert await sonos.find_tts_entity(None) == "tts.piper"
    out = await sonos.announce("Kitchen", "Middag är klar", 40, "tts.piper", dry_run=False)
    assert out["verified"] is None  # cannot be verified; reported honestly
    data = fake_ha.state.calls[-1]["data"]
    assert data["announce"] is True and data["extra"] == {"volume": 0.4}
    assert data["media_content_id"].startswith("media-source://tts/tts.piper?message=Middag%20%C3%A4r%20klar")


async def test_verification_failure_reported(fake_ha, monkeypatch):
    """If the device ignores a command, the result must say it was not verified."""
    inner = httpx.ASGITransport(app=fake_ha)

    class IgnoringTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            if "/api/services/" in request.url.path:
                return httpx.Response(200, json=[])  # accepted, but nothing changes
            return await inner.handle_async_request(request)

    sonos = SonosService(client(fake_ha, monkeypatch, transport=IgnoringTransport()), verify_delay_s=0.01, verify_timeout_s=0.05)
    out = await sonos.set_volume("kitchen", 70, dry_run=False)
    assert out["verified"] is False and out["volume"] == 25
