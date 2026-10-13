"""Push-to-talk voice input and local speech output, with fake engines.

No microphone, GPU, model or voice package is needed: every engine here is a
fake that records what the controller asked of it.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest
from conftest import ScriptedProvider
from fastapi.testclient import TestClient

from jarvis.config import VoiceConfig
from jarvis.core.orchestrator import SPOKEN_REPLY_HINT
from jarvis.providers.dev_provider import DevProvider
from jarvis.server.app import create_app
from jarvis.voice import engines, models
from jarvis.voice.speech_text import clean_for_speech, guess_language, split_sentences
from jarvis.voice.interfaces import (
    AudioCapture,
    AudioFrame,
    AudioPlayback,
    MicState,
    SpeechToText,
    TextToSpeech,
    Transcript,
    VoiceController,
    VoiceError,
)


class FakeCapture(AudioCapture):
    name = "fake-mic"

    def __init__(self, chunks: list[bytes] | None = None, fail: Exception | None = None) -> None:
        self.chunks = chunks if chunks is not None else [b"\x01\x00" * 160, b"\x02\x00" * 160]
        self.fail = fail
        self.is_open = False
        self.opened_with: list[str | None] = []
        self.closes = 0
        self._pending: list[bytes] = []

    async def devices(self) -> list[dict[str, Any]]:
        return [{"index": 1, "name": "Microphone (Realtek(R) Audio)", "hostapi": "MME", "channels": 2}]

    async def start(self, device: str | None) -> None:
        if self.fail:
            raise self.fail
        self.is_open = True
        self.opened_with.append(device)
        self._pending = list(self.chunks)

    async def stop(self) -> None:
        if self.is_open:
            self.closes += 1
        self.is_open = False

    async def frames(self) -> AsyncIterator[AudioFrame]:
        while self._pending:
            yield AudioFrame(self._pending.pop(0))


class FakeSTT(SpeechToText):
    name = "fake-whisper"
    is_cloud = False
    languages = ("sv", "en")

    def __init__(self, text: str = "Vad är klockan?", language: str = "sv", delay: float = 0.0) -> None:
        self.text, self.language, self.delay = text, language, delay
        self.received: list[bytes] = []
        self.prepared = 0

    async def prepare(self) -> None:
        self.prepared += 1

    async def transcribe(self, frames, language_hint=None) -> Transcript:
        async for f in frames:
            self.received.append(f.pcm)
        if self.delay:
            await asyncio.sleep(self.delay)
        return Transcript(self.text, self.language, 0.97)


class FakeTTS(TextToSpeech):
    name = "fake-piper"
    is_cloud = False

    def __init__(self, languages: tuple[str, ...] = ("sv",)) -> None:
        self.languages = languages
        self.calls: list[tuple[str, str]] = []

    async def synthesize(self, text: str, language: str) -> bytes:
        self.calls.append((text, language))
        return b"RIFF-fake"


class FakePlayback(AudioPlayback):
    def __init__(self, hold: float = 0.0) -> None:
        self.played: list[bytes] = []
        self.interrupted = 0
        self.hold = hold

    async def play(self, audio: bytes) -> None:
        self.played.append(audio)
        if self.hold:
            await asyncio.sleep(self.hold)

    async def interrupt(self) -> None:
        self.interrupted += 1


def make_voice(*, enabled: bool = True, max_capture_s: float = 30.0, **kw: Any) -> VoiceController:
    vc = VoiceController(max_capture_s=max_capture_s)
    vc.capture = kw.get("capture") or FakeCapture()
    vc.stt = kw.get("stt") or FakeSTT()
    vc.tts = kw.get("tts")
    vc.playback = kw.get("playback")
    vc.set_enabled(enabled)
    return vc


# ------------------------------------------------------------ controller ----
def test_no_engines_reports_not_implemented():
    vc = VoiceController()
    vc.unavailable["stt"] = "Whisper model 'large-v3-turbo' is not downloaded."
    assert vc.mic_state == MicState.NOT_IMPLEMENTED
    s = vc.status()
    assert s["mic_state"] == "not_implemented" and s["mode"] == "push_to_talk"
    assert s["stt"] == {"state": "not_implemented", "detail": "Whisper model 'large-v3-turbo' is not downloaded."}


async def test_master_switch_off_never_opens_microphone():
    cap = FakeCapture()
    vc = make_voice(enabled=False, capture=cap)
    assert vc.mic_state == MicState.DISABLED
    with pytest.raises(VoiceError, match="master switch"):
        await vc.begin_capture()
    assert cap.opened_with == [] and not cap.is_open


async def test_not_implemented_refuses_to_capture():
    vc = VoiceController()
    vc.set_enabled(True)
    with pytest.raises(VoiceError):
        await vc.begin_capture()


async def test_push_to_talk_opens_only_while_held_and_transcribes():
    cap, stt = FakeCapture(), FakeSTT(delay=0.01)
    vc = make_voice(capture=cap, stt=stt)
    seen: list[str] = []
    vc.on_change = lambda: seen.append(vc.mic_state.value)

    assert vc.mic_state == MicState.IDLE and not cap.is_open
    await vc.begin_capture("Microphone (Realtek(R) Audio)")
    assert cap.is_open and vc.mic_state == MicState.CAPTURING
    assert cap.opened_with == ["Microphone (Realtek(R) Audio)"]

    transcript, duration = await vc.end_capture()
    assert not cap.is_open and cap.closes == 1
    assert transcript.text == "Vad är klockan?" and transcript.language == "sv"
    assert duration >= 0
    assert stt.received == cap.chunks  # every recorded frame reached speech recognition
    assert vc.mic_state == MicState.IDLE
    assert seen[0] == "capturing" and "processing" in seen and seen[-1] == "idle"


async def test_model_loads_while_user_is_talking():
    stt = FakeSTT()
    vc = make_voice(stt=stt)
    await vc.begin_capture()
    await asyncio.sleep(0)
    assert stt.prepared == 1
    await vc.cancel_capture()


async def test_second_press_while_listening_is_refused():
    vc = make_voice()
    await vc.begin_capture()
    with pytest.raises(VoiceError, match="Already listening"):
        await vc.begin_capture()
    await vc.cancel_capture()


async def test_turning_master_switch_off_closes_microphone_and_discards_audio():
    cap, stt = FakeCapture(), FakeSTT()
    vc = make_voice(capture=cap, stt=stt)
    await vc.begin_capture()
    vc.set_enabled(False)
    await asyncio.sleep(0.01)
    assert not cap.is_open and vc.mic_state == MicState.DISABLED
    assert stt.received == []
    with pytest.raises(VoiceError):
        await vc.end_capture()


async def test_capture_limit_closes_microphone_even_if_held():
    cap = FakeCapture()
    vc = make_voice(capture=cap, max_capture_s=0.05)
    await vc.begin_capture()
    await asyncio.sleep(0.15)
    assert not cap.is_open and vc.mic_state == MicState.IDLE
    assert "limit" in (vc.last_error or "")
    with pytest.raises(VoiceError, match="limit"):
        await vc.end_capture()


async def test_cancel_discards_recording():
    cap, stt = FakeCapture(), FakeSTT()
    vc = make_voice(capture=cap, stt=stt)
    await vc.begin_capture()
    assert await vc.cancel_capture() is True
    assert not cap.is_open and stt.received == []
    assert await vc.cancel_capture() is False


async def test_microphone_error_is_reported_not_hidden():
    vc = make_voice(capture=FakeCapture(fail=OSError("device busy")))
    with pytest.raises(VoiceError, match="device busy"):
        await vc.begin_capture()
    assert vc.mic_state == MicState.IDLE and "device busy" in vc.last_error


async def test_speech_recognition_failure_returns_to_idle():
    class BrokenSTT(FakeSTT):
        async def transcribe(self, frames, language_hint=None):
            raise RuntimeError("CUDA out of memory")

    vc = make_voice(stt=BrokenSTT())
    await vc.begin_capture()
    with pytest.raises(VoiceError, match="CUDA out of memory"):
        await vc.end_capture()
    assert vc.mic_state == MicState.IDLE


async def test_speak_uses_local_tts_and_playback():
    tts, pb = FakeTTS(), FakePlayback()
    vc = make_voice(tts=tts, playback=pb)
    assert await vc.speak("Hej!", "sv") is True
    assert tts.calls == [("Hej!", "sv")] and pb.played == [b"RIFF-fake"]
    # A language without its own voice falls back to the first configured one.
    await vc.speak("Hello", "en")
    assert tts.calls[-1] == ("Hello", "sv")
    assert await VoiceController().speak("x", "sv") is False
    assert vc.speaking is False


async def test_english_reply_uses_english_voice_when_present():
    tts = FakeTTS(languages=("sv", "en"))
    vc = make_voice(tts=tts, playback=FakePlayback())
    await vc.speak("The kitchen speaker is now paused.")
    await vc.speak("Musiken i köket är pausad.")
    assert [lang for _, lang in tts.calls] == ["en", "sv"]


async def test_markdown_is_removed_before_speech():
    tts = FakeTTS()
    vc = make_voice(tts=tts, playback=FakePlayback())
    await vc.speak("**Klart!** 🎵 Här är `koden`:\n```python\nprint(1)\n```")
    spoken = " ".join(t for t, _ in tts.calls)
    for junk in ("**", "`", "🎵", "print(1)", "```"):
        assert junk not in spoken
    assert spoken.startswith("Klart!")


async def test_long_reply_is_read_sentence_by_sentence_and_stops_on_interrupt():
    tts, pb = FakeTTS(), FakePlayback(hold=0.05)
    vc = make_voice(tts=tts, playback=pb)
    task = asyncio.create_task(vc.speak("Första meningen. Andra meningen. Tredje meningen. Fjärde meningen."))
    await asyncio.sleep(0.07)
    assert vc.speaking
    assert await vc.interrupt_speech() is True
    assert await task is False
    assert vc.speaking is False and pb.interrupted == 1
    assert len(pb.played) < 4  # the rest of the reply was never played


async def test_pressing_the_button_interrupts_speech():
    pb = FakePlayback(hold=0.2)
    vc = make_voice(tts=FakeTTS(), playback=pb)
    task = asyncio.create_task(vc.speak("Ett långt svar", "sv"))
    await asyncio.sleep(0.02)
    assert vc.speaking
    await vc.begin_capture()
    assert pb.interrupted == 1 and not vc.speaking
    await vc.cancel_capture()
    await task


async def test_new_reply_replaces_the_one_being_read():
    pb = FakePlayback(hold=0.1)
    tts = FakeTTS()
    vc = make_voice(tts=tts, playback=pb)
    first = asyncio.create_task(vc.speak("Ett. Två. Tre."))
    await asyncio.sleep(0.02)
    second = asyncio.create_task(vc.speak("Nytt svar."))
    assert await first is False
    assert await second is True
    assert tts.calls[-1][0] == "Nytt svar."


# ------------------------------------------------------------- speech text ----
def test_clean_for_speech():
    text = (
        "[Dev fallback · rule-based] # Status\n"
        "Här är **tre** saker:\n"
        "- Köket spelar _jazz_\n"
        "- Vardagsrummet är pausat\n"
        "1. Se [dokumentationen](https://example.com) 👍\n"
        "| Rum | Volym |\n|---|---|\n| Kök | 30 |\n"
        "```\ncode()\n```\n"
        "Läs mer på https://example.com"
    )
    out = clean_for_speech(text)
    assert out.startswith("Status Här är tre saker: Köket spelar jazz. Vardagsrummet är pausat. Se dokumentationen.")
    for junk in ("#", "**", "_", "- ", "https://", "👍", "code()", "```", "|", "[", "Dev fallback"):
        assert junk not in out
    assert clean_for_speech("🎉✨") == ""


def test_split_sentences():
    assert split_sentences("Hej. Klockan är tre! Vill du något mer?") == ["Hej.", "Klockan är tre!", "Vill du något mer?"]
    long = "ord, " * 80
    assert all(len(s) <= 241 for s in split_sentences(long))


def test_guess_language():
    assert guess_language("Klockan är 14:05.") == "sv"
    assert guess_language("Musiken i köket är pausad.") == "sv"
    assert guess_language("It is five past two and the music is paused.") == "en"


# ------------------------------------------------------------ voice replies ----
@pytest.mark.parametrize(
    "voice_input, speak_replies, voice_mode, expected",
    [
        (True, False, False, True),  # asked with the microphone: always spoken
        (False, False, False, False),  # typed: follows the setting (off)
        (False, True, False, True),  # typed with "Read replies aloud"
        (False, False, True, True),  # voice mode reads every reply
    ],
)
def test_when_replies_are_spoken(make_core, voice_input, speak_replies, voice_mode, expected):
    core = make_core(voice=make_voice(tts=FakeTTS(), playback=FakePlayback()))
    core.settings.update({"speak_replies": speak_replies, "voice_mode": voice_mode})
    assert core.reply_is_spoken(voice_input) is expected


async def test_spoken_turn_asks_for_speech_style_only_after_tools_ran(make_core):
    seen: list[str] = []  # the user turn's context as each model round saw it

    def tool_round(messages):
        seen.append(next(m for m in reversed(messages) if m.role == "user").context)
        return [("system_get_time", {})]

    def answer(messages):
        seen.append(next(m for m in reversed(messages) if m.role == "user").context)
        return "Klockan är tre."

    model = ScriptedProvider("anthropic", "cloud", [tool_round, answer, tool_round, answer])
    core = make_core({"anthropic": model}, voice=make_voice(), ai={"routes": {"conversation": [{"provider": "anthropic"}]}})
    r = await core.chat("Vad är klockan?", voice_input=True)
    assert r["speech"] is True
    # Round 1 (choosing tools) looks exactly like a typed request; the answer round gets the hint.
    assert SPOKEN_REPLY_HINT not in seen[0] and SPOKEN_REPLY_HINT in seen[1]
    r = await core.chat("Vad är klockan?")
    assert r["speech"] is False
    assert SPOKEN_REPLY_HINT not in seen[2] and SPOKEN_REPLY_HINT not in seen[3]
    # The system prompt (cached) is the same in every round.
    assert len({req["system"] for req in model.requests}) == 1


# ------------------------------------------------------------------ wiring ----
def test_build_controller_reports_missing_models(tmp_path, monkeypatch):
    monkeypatch.setattr(engines, "SoundDeviceCapture", lambda default_device: FakeCapture())
    vc = engines.build_voice_controller(VoiceConfig(), tmp_path / "models")
    assert vc.capture is not None and vc.stt is None and vc.tts is None
    assert "voice-models --download" in vc.unavailable["stt"]
    assert "voice-models --download" in vc.unavailable["tts"]
    assert vc.mic_state == MicState.NOT_IMPLEMENTED


def test_build_controller_respects_disabled_config(tmp_path):
    vc = engines.build_voice_controller(VoiceConfig(enabled=False), tmp_path)
    assert vc.capture is None and vc.stt is None
    assert "disabled" in vc.unavailable["stt"]


def test_model_paths():
    assert models.piper_repo_path("sv_SE-nst-medium") == "sv/sv_SE/nst/medium/sv_SE-nst-medium"
    assert models.whisper_model_dir(__import__("pathlib").Path("/m"), "large-v3-turbo").parts[-2:] == ("whisper", "large-v3-turbo")


def test_resampling_to_16k_mono():
    np = pytest.importorskip("numpy")
    stereo_48k = (np.ones((4800, 2), dtype=np.int16) * 16384).tobytes()  # 0.1 s
    out = engines.to_mono_16k(stereo_48k, 48000, 2)
    assert out.shape == (1600,) and out.dtype == np.float32
    assert abs(float(out.mean()) - 0.5) < 1e-3
    mono_44k = (np.zeros(4410, dtype=np.int16)).tobytes()
    assert engines.to_mono_16k(mono_44k, 44100, 1).shape == (1600,)


def test_settings_default_microphone_comes_from_config(make_core):
    core = make_core(voice=make_voice())
    assert core.settings.current.microphone_device == "Microphone (Realtek(R) Audio)"


async def _wait_for(cond, tries: int = 100) -> None:
    for _ in range(tries):
        if cond():
            return
        await asyncio.sleep(0.01)


async def test_spoken_request_is_answered_aloud_without_the_setting(make_core):
    tts, pb = FakeTTS(), FakePlayback()
    providers = {"anthropic": ScriptedProvider("anthropic", "cloud", ["**Klockan** är tre.", "Klockan är fyra."])}
    core = make_core(providers, voice=make_voice(tts=tts, playback=pb), ai={"routes": {"conversation": [{"provider": "anthropic"}]}})
    await core.start()
    try:
        assert core.settings.current.speak_replies is False
        await core.chat("Vad är klockan?", voice_input=True)
        await _wait_for(lambda: pb.played)
        assert tts.calls == [("Klockan är tre.", "sv")]  # cleaned of markdown
        await core.chat("Och nu?")  # typed, setting off: not read aloud
        await asyncio.sleep(0.05)
        assert len(tts.calls) == 1
    finally:
        await core.stop()


# ------------------------------------------------------------------ server ----
TOKEN = "t" * 40
H = {"X-Jarvis-Token": TOKEN}


@pytest.fixture
def voice_client(make_core):
    providers = {
        "anthropic": ScriptedProvider("anthropic", "cloud", [], available=False),
        "ollama": ScriptedProvider("ollama", "local", [], available=False),
        "dev": DevProvider(),
    }
    cap, stt = FakeCapture(), FakeSTT(text="Öppna anteckningar")
    core = make_core(providers, voice=make_voice(enabled=False, capture=cap, stt=stt))
    app = create_app(core, token=TOKEN, extra_hosts={"testserver"})
    with TestClient(app) as c:
        c.core, c.cap, c.stt = core, cap, stt
        yield c


def test_voice_endpoints_require_token(voice_client):
    assert voice_client.post("/api/voice/start").status_code == 401
    assert voice_client.get("/api/voice/devices").status_code == 401


def test_start_refused_while_master_switch_off(voice_client):
    r = voice_client.post("/api/voice/start", json={}, headers=H)
    assert r.status_code == 409 and "master switch" in r.json()["detail"]
    assert voice_client.cap.opened_with == []
    assert voice_client.get("/api/state", headers=H).json()["voice"]["mic_state"] == "disabled"


def test_push_to_talk_round_trip(voice_client):
    assert voice_client.patch("/api/settings", json={"microphone_enabled": True}, headers=H).status_code == 200
    r = voice_client.post("/api/voice/start", json={}, headers=H)
    assert r.status_code == 200 and r.json()["mic_state"] == "capturing"
    assert voice_client.cap.is_open
    assert voice_client.cap.opened_with == ["Microphone (Realtek(R) Audio)"]  # the default device
    r = voice_client.post("/api/voice/stop", headers=H)
    assert r.status_code == 200
    body = r.json()
    assert body["text"] == "Öppna anteckningar" and body["language"] == "sv"
    assert not voice_client.cap.is_open
    assert voice_client.get("/api/state", headers=H).json()["voice"]["mic_state"] == "idle"
    # Releasing again without pressing is an error, not a silent success.
    assert voice_client.post("/api/voice/stop", headers=H).status_code == 409


def test_selected_microphone_is_used(voice_client):
    voice_client.patch("/api/settings", json={"microphone_enabled": True, "microphone_device": "USB Mic"}, headers=H)
    voice_client.post("/api/voice/start", json={}, headers=H)
    assert voice_client.cap.opened_with[-1] == "USB Mic"
    assert voice_client.post("/api/voice/cancel", headers=H).json() == {"cancelled": True}
    assert not voice_client.cap.is_open


def test_device_list(voice_client):
    r = voice_client.get("/api/voice/devices", headers=H).json()
    assert r["selected"] == "Microphone (Realtek(R) Audio)"
    assert r["devices"][0]["name"] == "Microphone (Realtek(R) Audio)"


def test_switching_master_switch_off_closes_open_microphone(voice_client):
    voice_client.patch("/api/settings", json={"microphone_enabled": True}, headers=H)
    voice_client.post("/api/voice/start", json={}, headers=H)
    assert voice_client.cap.is_open
    voice_client.patch("/api/settings", json={"microphone_enabled": False}, headers=H)
    voice_client.get("/api/state", headers=H)  # let the event loop run the close
    assert not voice_client.cap.is_open


def test_chat_marks_spoken_requests(voice_client):
    r = voice_client.post("/api/chat", json={"text": "vad är klockan", "voice": True}, headers=H).json()
    assert r["ok"] and r["speech"] is True
    r = voice_client.post("/api/chat", json={"text": "vad är klockan"}, headers=H).json()
    assert r["speech"] is False
    voice_client.patch("/api/settings", json={"voice_mode": True}, headers=H)
    assert voice_client.post("/api/chat", json={"text": "vad är klockan"}, headers=H).json()["speech"] is True


def test_interrupt_endpoint_works_with_microphone_off(voice_client):
    vc = voice_client.core.voice
    assert vc.enabled is False
    vc.speaking = True  # as if a reply were being read
    vc.playback = FakePlayback()
    assert voice_client.post("/api/voice/interrupt", headers=H).json() == {"interrupted": True}
    assert vc.speaking is False and vc.playback.interrupted == 1
    assert voice_client.post("/api/voice/interrupt", headers=H).json() == {"interrupted": False}


def test_interface_disconnect_closes_microphone(voice_client):
    voice_client.patch("/api/settings", json={"microphone_enabled": True}, headers=H)
    with voice_client.websocket_connect(f"/ws?token={TOKEN}", headers={"origin": "http://testserver"}) as ws:
        ws.receive_json()
        voice_client.post("/api/voice/start", json={}, headers=H)
        assert voice_client.cap.is_open
    voice_client.get("/api/state", headers=H)
    assert not voice_client.cap.is_open
