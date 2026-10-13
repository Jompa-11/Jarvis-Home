"""Text-to-speech backends behind one ``TextToSpeech``: Piper, Kokoro and Azure.

A voice is named by a short spec (see ``parse_voice``). Each reply language has
a voice and a local fallback; if a cloud voice fails, has no key, or reaches
its monthly character limit, the local voice speaks instead, automatically.
The optional JARVIS filter (``effects.JarvisFilter``) runs after synthesis.

Every backend returns a WAV file as bytes, built in memory.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import io
import json
import logging
import threading
import wave
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape

from ..config import AzureSpeechConfig
from .interfaces import TextToSpeech
from .models import kokoro_files, piper_voice_path

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class VoiceSpec:
    engine: str  # "piper" | "kokoro" | "azure"
    name: str
    speaker: str | None = None

    @property
    def label(self) -> str:
        return f"{self.engine}:{self.name}" + (f"#{self.speaker}" if self.speaker else "")


def parse_voice(spec: str) -> VoiceSpec:
    """'sv_SE-nst-medium', 'piper:en_GB-vctk-medium#p232', 'kokoro:bm_george', 'azure:en-GB-RyanNeural'."""
    spec = spec.strip()
    engine, sep, rest = spec.partition(":")
    if not sep or len(engine) == 1:  # no prefix (a drive letter such as C: is not an engine)
        engine, rest = "piper", spec
    engine = engine.lower()
    if engine not in ("piper", "kokoro", "azure"):
        raise ValueError(f"unknown voice engine '{engine}' in '{spec}'")
    name, _, speaker = rest.partition("#")
    if not name:
        raise ValueError(f"voice name missing in '{spec}'")
    return VoiceSpec(engine, name, speaker or None)


def pcm_to_wav(pcm: bytes, rate: int) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)
    return buf.getvalue()


class VoiceUnavailable(Exception):
    """This backend cannot speak right now (missing key, limit reached, ...)."""


# ------------------------------------------------------------------ backends ----
class PiperBackend:
    is_cloud = False

    def __init__(self, spec: VoiceSpec, models_dir: Path) -> None:
        import piper  # noqa: F401

        self.spec = spec
        self.path = piper_voice_path(models_dir, spec.name)
        if not self.path.exists():
            raise FileNotFoundError(f"Piper voice '{spec.name}' is not downloaded")
        self._voice: Any = None
        self._speaker_id: int | None = None
        self._lock = threading.Lock()

    def _load(self) -> Any:
        with self._lock:
            if self._voice is None:
                from piper import PiperVoice

                self._voice = PiperVoice.load(self.path)
                if self.spec.speaker is not None:
                    ids = self._voice.config.speaker_id_map or {}
                    if self.spec.speaker in ids:
                        self._speaker_id = ids[self.spec.speaker]
                    elif self.spec.speaker.isdigit():
                        self._speaker_id = int(self.spec.speaker)
                    else:
                        raise ValueError(f"speaker '{self.spec.speaker}' not in {self.spec.name}")
            return self._voice

    def synth(self, text: str) -> bytes:
        voice = self._load()
        syn = None
        if self._speaker_id is not None:
            from piper import SynthesisConfig

            syn = SynthesisConfig(speaker_id=self._speaker_id)
        pcm = b"".join(chunk.audio_int16_bytes for chunk in voice.synthesize(text, syn_config=syn))
        return pcm_to_wav(pcm, voice.config.sample_rate)


class KokoroBackend:
    """Kokoro-82M (Apache-2.0) via kokoro-onnx. English voices only."""

    is_cloud = False
    _shared: dict[Path, Any] = {}
    _shared_lock = threading.Lock()

    def __init__(self, spec: VoiceSpec, models_dir: Path) -> None:
        import kokoro_onnx  # noqa: F401

        self.spec = spec
        self.model_path, self.voices_path = kokoro_files(models_dir)
        if not (self.model_path.exists() and self.voices_path.exists()):
            raise FileNotFoundError("Kokoro model is not downloaded")
        # bm_/bf_ = British, am_/af_ = American
        self.lang = "en-gb" if spec.name.startswith("b") else "en-us"

    def _engine(self) -> Any:
        with self._shared_lock:
            if self.model_path not in self._shared:
                from kokoro_onnx import Kokoro

                self._shared[self.model_path] = Kokoro(str(self.model_path), str(self.voices_path))
            return self._shared[self.model_path]

    def synth(self, text: str) -> bytes:
        import numpy as np

        samples, rate = self._engine().create(text, voice=self.spec.name, speed=1.0, lang=self.lang)
        pcm = (np.clip(samples, -1.0, 1.0) * 32767.0).astype(np.int16).tobytes()
        return pcm_to_wav(pcm, rate)


class AzureUsage:
    """Characters sent to Azure this month, stored as a count only (no text)."""

    def __init__(self, path: Path | None, limit: int) -> None:
        self.path, self.limit = path, limit
        self._lock = threading.Lock()
        self.month = dt.date.today().strftime("%Y-%m")
        self.chars = 0
        if path and path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                if data.get("month") == self.month:
                    self.chars = int(data.get("azure_chars", 0))
            except (OSError, ValueError):
                log.warning("Voice usage file is unreadable; starting from zero")

    def reserve(self, n: int) -> None:
        with self._lock:
            month = dt.date.today().strftime("%Y-%m")
            if month != self.month:
                self.month, self.chars = month, 0
            if self.chars + n > self.limit:
                raise VoiceUnavailable(f"Azure monthly character limit reached ({self.chars}/{self.limit})")
            self.chars += n
            if self.path:
                try:
                    self.path.parent.mkdir(parents=True, exist_ok=True)
                    self.path.write_text(json.dumps({"month": self.month, "azure_chars": self.chars}), encoding="utf-8")
                except OSError:
                    log.warning("Could not save voice usage")


class AzureBackend:
    """Azure neural voice through the Speech REST API (no SDK)."""

    is_cloud = True

    def __init__(self, spec: VoiceSpec, cfg: AzureSpeechConfig, usage: AzureUsage, *, key: str | None, region: str | None, transport: Any = None) -> None:
        self.spec, self.cfg, self.usage = spec, cfg, usage
        self.key, self.region = key, region
        self.locale = "-".join(spec.name.split("-")[:2])  # en-GB-RyanNeural -> en-GB
        self._transport = transport

    def synth(self, text: str) -> bytes:
        import httpx

        if not self.key or not self.region:
            raise VoiceUnavailable(f"{self.cfg.key_env} / {self.cfg.region_env} are not set")
        self.usage.reserve(len(text))
        ssml = f"<speak version='1.0' xml:lang='{self.locale}'><voice name='{escape(self.spec.name)}'>{escape(text)}</voice></speak>"
        with httpx.Client(transport=self._transport, timeout=self.cfg.timeout_s) as client:
            r = client.post(
                f"https://{self.region}.tts.speech.microsoft.com/cognitiveservices/v1",
                content=ssml.encode("utf-8"),
                headers={
                    "Ocp-Apim-Subscription-Key": self.key,
                    "Content-Type": "application/ssml+xml",
                    "X-Microsoft-OutputFormat": "riff-24khz-16bit-mono-pcm",
                    "User-Agent": "jarvis-home",
                },
            )
        if r.status_code != 200:
            raise RuntimeError(f"Azure speech returned HTTP {r.status_code}")
        return r.content


# ------------------------------------------------------------------- facade ----
class VoiceTTS(TextToSpeech):
    """One voice (and a local fallback) per language, plus the optional filter."""

    name = "voices"

    def __init__(self, voices: dict[str, Any], fallbacks: dict[str, Any] | None = None, effects: Callable[[bytes], bytes] | None = None) -> None:
        if not voices:
            raise ValueError("no voice available")
        self.voices = voices
        self.fallbacks = fallbacks or {}
        self.effects = effects
        self.languages = tuple(voices)
        self.last_fallback: str | None = None

    @property
    def is_cloud(self) -> bool:  # type: ignore[override]
        return any(getattr(b, "is_cloud", False) for b in self.voices.values())

    def _synth(self, text: str, language: str, effects: bool | None) -> bytes:
        backend = self.voices.get(language) or self.voices[self.languages[0]]
        try:
            wav = backend.synth(text)
        except Exception as exc:
            fallback = self.fallbacks.get(language)
            if fallback is None or fallback is backend:
                raise
            self.last_fallback = f"{backend.spec.label} failed ({exc}); used {fallback.spec.label}"
            log.warning("Voice %s unavailable (%s); falling back to %s", backend.spec.label, exc, fallback.spec.label)
            wav = fallback.synth(text)
        use_effects = self.effects is not None if effects is None else effects
        if use_effects and self.effects is not None:
            wav = self.effects(wav)
        return wav

    async def synthesize(self, text: str, language: str, *, effects: bool | None = None) -> bytes:
        return await asyncio.to_thread(self._synth, text, language, effects)

    def describe(self) -> dict[str, Any]:
        return {
            "voices": {lang: b.spec.label for lang, b in self.voices.items()},
            "fallbacks": {lang: b.spec.label for lang, b in self.fallbacks.items()},
            "filter": "on" if self.effects is not None else "off",
            "last_fallback": self.last_fallback,
        }


def make_backend(spec: str, models_dir: Path, azure: AzureSpeechConfig, usage: AzureUsage, *, key: str | None = None, region: str | None = None, transport: Any = None) -> Any:
    vs = parse_voice(spec)
    if vs.engine == "piper":
        return PiperBackend(vs, models_dir)
    if vs.engine == "kokoro":
        return KokoroBackend(vs, models_dir)
    return AzureBackend(vs, azure, usage, key=key, region=region, transport=transport)
