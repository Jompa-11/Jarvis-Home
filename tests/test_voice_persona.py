"""Voice choice, Azure with automatic Piper fallback, the JARVIS filter and the persona.

All engines are fakes or mocked HTTP: no audio device, model, network or key is used.
"""

from __future__ import annotations

import datetime as dt
import io
import json
import wave

import httpx
import pytest

from jarvis.config import AzureSpeechConfig, JarvisConfig, VoiceConfig, VoiceEffectsConfig
from jarvis.core.orchestrator import build_persona_section, build_system_prompt
from jarvis.voice import engines, models, tts
from jarvis.voice.audition import SENTENCES, spoken_name
from jarvis.voice.tts import AzureBackend, AzureUsage, VoiceTTS, VoiceUnavailable, parse_voice


def wav(seconds: float = 0.1, rate: int = 22050) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"\x10\x00" * int(seconds * rate))
    return buf.getvalue()


class FakeBackend:
    def __init__(self, label: str, fail: Exception | None = None, cloud: bool = False) -> None:
        self.spec = parse_voice(label)
        self.fail = fail
        self.is_cloud = cloud
        self.calls: list[str] = []

    def synth(self, text: str) -> bytes:
        self.calls.append(text)
        if self.fail:
            raise self.fail
        return wav()


# --------------------------------------------------------------- voice specs ----
def test_parse_voice_specs():
    assert parse_voice("sv_SE-nst-medium") == tts.VoiceSpec("piper", "sv_SE-nst-medium")
    assert parse_voice("piper:en_GB-vctk-medium#p232") == tts.VoiceSpec("piper", "en_GB-vctk-medium", "p232")
    assert parse_voice("kokoro:bm_george").engine == "kokoro"
    assert parse_voice("azure:en-GB-RyanNeural") == tts.VoiceSpec("azure", "en-GB-RyanNeural")
    assert parse_voice(r"C:\voices\x.onnx").engine == "piper"  # a drive letter is not an engine
    with pytest.raises(ValueError):
        parse_voice("elevenlabs:someone")
    with pytest.raises(ValueError):
        parse_voice("azure:")


def test_local_voice_names_skip_cloud_voices():
    names, kokoro = models.local_voice_names(["sv_SE-nst-medium", "azure:sv-SE-MattiasNeural", "piper:en_GB-vctk-medium#p226", "kokoro:bm_george", "sv_SE-nst-medium"])
    assert names == ["sv_SE-nst-medium", "en_GB-vctk-medium"] and kokoro is True


def test_spoken_names_and_test_sentences():
    assert spoken_name("piper:en_GB-vctk-medium#p232") == "Piper vctk, speaker p232"
    assert spoken_name("kokoro:bm_george") == "Kokoro george"
    assert spoken_name("azure:sv-SE-MattiasNeural") == "Azure Mattias"
    assert SENTENCES["en"].startswith("Good morning, sir.") and SENTENCES["sv"].startswith("God morgon, sir.")


# ------------------------------------------------------------ fallback facade ----
async def test_cloud_failure_falls_back_to_local_voice():
    cloud = FakeBackend("azure:en-GB-RyanNeural", fail=RuntimeError("HTTP 503"), cloud=True)
    local = FakeBackend("piper:en_GB-alan-medium")
    t = VoiceTTS({"en": cloud}, {"en": local})
    out = await t.synthesize("Good evening.", "en")
    assert out.startswith(b"RIFF") and local.calls == ["Good evening."]
    assert "used piper:en_GB-alan-medium" in t.describe()["last_fallback"]
    assert t.is_cloud is True


async def test_local_failure_without_fallback_raises():
    t = VoiceTTS({"sv": FakeBackend("sv_SE-nst-medium", fail=RuntimeError("broken"))})
    with pytest.raises(RuntimeError):
        await t.synthesize("Hej", "sv")


async def test_filter_runs_after_synthesis_and_can_be_skipped():
    applied: list[bytes] = []

    def fx(data: bytes) -> bytes:
        applied.append(data)
        return b"FILTERED"

    t = VoiceTTS({"sv": FakeBackend("sv_SE-nst-medium")}, effects=fx)
    assert await t.synthesize("Hej", "sv") == b"FILTERED"
    assert (await t.synthesize("Hej", "sv", effects=False)).startswith(b"RIFF")
    assert len(applied) == 1 and t.describe()["filter"] == "on"


async def test_unknown_language_uses_first_voice():
    sv = FakeBackend("sv_SE-nst-medium")
    t = VoiceTTS({"sv": sv})
    await t.synthesize("Bonjour", "fr")
    assert sv.calls == ["Bonjour"]


# --------------------------------------------------------------------- azure ----
def azure(transport, *, key="k" * 32, region="swedencentral", limit=1000, path=None):
    usage = AzureUsage(path, limit)
    return AzureBackend(parse_voice("azure:sv-SE-MattiasNeural"), AzureSpeechConfig(monthly_char_limit=limit), usage, key=key, region=region, transport=transport), usage


def test_azure_request_shape_and_escaping():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["headers"] = dict(request.headers)
        seen["body"] = request.content.decode()
        return httpx.Response(200, content=wav())

    backend, usage = azure(httpx.MockTransport(handler))
    out = backend.synth("Fisk & <chips>")
    assert out.startswith(b"RIFF")
    assert seen["url"] == "https://swedencentral.tts.speech.microsoft.com/cognitiveservices/v1"
    assert seen["headers"]["ocp-apim-subscription-key"] == "k" * 32
    assert seen["headers"]["x-microsoft-outputformat"] == "riff-24khz-16bit-mono-pcm"
    assert "xml:lang='sv-SE'" in seen["body"] and "name='sv-SE-MattiasNeural'" in seen["body"]
    assert "Fisk &amp; &lt;chips&gt;" in seen["body"]
    assert usage.chars == len("Fisk & <chips>")


def test_azure_without_key_is_unavailable_and_sends_nothing():
    sent = []
    backend, usage = azure(httpx.MockTransport(lambda r: sent.append(r) or httpx.Response(200)), key=None)
    with pytest.raises(VoiceUnavailable):
        backend.synth("Hej")
    assert sent == [] and usage.chars == 0


def test_azure_monthly_limit_stops_cloud_use(tmp_path):
    sent = []
    path = tmp_path / "voice_usage.json"
    backend, usage = azure(httpx.MockTransport(lambda r: sent.append(r) or httpx.Response(200, content=wav())), limit=10, path=path)
    backend.synth("12345")
    with pytest.raises(VoiceUnavailable, match="limit"):
        backend.synth("1234567")
    assert len(sent) == 1
    stored = json.loads(path.read_text(encoding="utf-8"))
    assert stored == {"month": dt.date.today().strftime("%Y-%m"), "azure_chars": 5}  # a count only, no text
    assert AzureUsage(path, 10).chars == 5  # survives a restart


def test_azure_usage_resets_in_a_new_month(tmp_path):
    path = tmp_path / "voice_usage.json"
    path.write_text(json.dumps({"month": "2000-01", "azure_chars": 999}), encoding="utf-8")
    assert AzureUsage(path, 1000).chars == 0


def test_azure_http_error_raises_for_fallback():
    backend, _ = azure(httpx.MockTransport(lambda r: httpx.Response(401)))
    with pytest.raises(RuntimeError, match="401"):
        backend.synth("Hej")


async def test_azure_error_speaks_with_piper_end_to_end():
    backend, _ = azure(httpx.MockTransport(lambda r: httpx.Response(500)))
    local = FakeBackend("sv_SE-nst-medium")
    t = VoiceTTS({"sv": backend}, {"sv": local})
    assert (await t.synthesize("God morgon, sir.", "sv")).startswith(b"RIFF")
    assert local.calls == ["God morgon, sir."]


# ---------------------------------------------------------------- build_tts ----
def test_build_tts_uses_fallback_when_voice_missing(tmp_path, monkeypatch):
    def fake_make(spec, *a, **k):
        if spec.startswith("kokoro"):
            raise FileNotFoundError("Kokoro model is not downloaded")
        return FakeBackend(spec)

    monkeypatch.setattr(engines, "make_backend", fake_make)
    cfg = VoiceConfig(tts_voice_en="kokoro:bm_george")
    t = engines.build_tts(cfg, tmp_path / "models")
    assert t.describe()["voices"] == {"sv": "piper:sv_SE-nst-medium", "en": "piper:en_GB-alan-medium"}
    assert t.describe()["filter"] == "off"


def test_build_tts_keeps_cloud_voice_without_key(tmp_path, monkeypatch, ):
    monkeypatch.delenv("AZURE_SPEECH_KEY", raising=False)
    real = engines.make_backend
    monkeypatch.setattr(engines, "make_backend", lambda spec, *a, **k: real(spec, *a, **k) if spec.startswith("azure") else FakeBackend(spec))
    t = engines.build_tts(VoiceConfig(tts_voice_sv="azure:sv-SE-MattiasNeural"), tmp_path / "models")
    assert t.describe()["voices"]["sv"] == "azure:sv-SE-MattiasNeural"
    assert t.describe()["fallbacks"]["sv"] == "piper:sv_SE-nst-medium"


# ------------------------------------------------------------------ effects ----
def test_effects_config_bounds():
    assert VoiceEffectsConfig().enabled is False  # opt-in
    with pytest.raises(ValueError):
        VoiceEffectsConfig(pitch_semitones=-12)


def test_jarvis_filter_processes_audio_in_memory():
    pytest.importorskip("pedalboard")
    np = pytest.importorskip("numpy")
    from jarvis.voice.effects import JarvisFilter

    rate = 22050
    t = np.arange(rate) / rate
    tone = (0.3 * np.sin(2 * np.pi * 220 * t) * 32767).astype(np.int16).tobytes()
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(tone)
    out = JarvisFilter(VoiceEffectsConfig(enabled=True))(buf.getvalue())
    with wave.open(io.BytesIO(out)) as w:
        assert w.getframerate() == rate and w.getnchannels() == 1
        frames = w.getnframes()
        data = np.frombuffer(w.readframes(frames), dtype=np.int16)
    assert frames > rate  # the reverb tail was kept
    assert np.abs(data).max() < 32767  # the limiter prevents clipping


# ------------------------------------------------------------------ persona ----
def test_persona_is_appended_and_rules_are_unchanged():
    cfg = JarvisConfig()
    rules = build_system_prompt(cfg)
    persona = build_persona_section(cfg)
    assert "sir" in persona and "every rule above takes precedence" in persona
    off = JarvisConfig.model_validate({"persona": {"enabled": False}})
    assert build_persona_section(off) == ""
    assert build_system_prompt(off) == rules  # the safety and tool rules do not depend on the persona


def test_orchestrator_prompt_is_rules_then_persona(make_core):
    core = make_core()
    prompt = core.orchestrator.system_prompt
    rules = build_system_prompt(core.cfg)
    assert prompt.startswith(rules) and prompt[len(rules):] == build_persona_section(core.cfg)
