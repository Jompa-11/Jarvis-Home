"""Local voice engines: microphone (PortAudio), Whisper (faster-whisper) and Piper.

Heavy packages are imported lazily, so the rest of JARVIS (and the test suite)
runs without them. Audio is kept in memory only: frames go from the sound card
into a queue, then straight into Whisper as an array. Nothing is written to
disk and nothing leaves the machine. Models are loaded from ``data/models`` with
downloads disabled, so a missing model is reported instead of fetched silently.
"""

from __future__ import annotations

import asyncio
import io
import logging
import os
import sys
import threading
import wave
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from ..config import VoiceConfig
from .interfaces import AudioCapture, AudioFrame, AudioPlayback, SpeechToText, Transcript, VoiceController
from .models import whisper_model_dir
from .tts import AzureUsage, VoiceTTS, make_backend

log = logging.getLogger(__name__)

TARGET_RATE = 16000
# Host APIs in order of preference. WASAPI reports full device names and the
# device's native rate; MME truncates names to 31 characters.
HOSTAPI_PREFERENCE = ("Windows WASAPI", "MME", "Windows DirectSound", "ALSA", "Core Audio", "PulseAudio")


def to_mono_16k(pcm: bytes, rate: int, channels: int) -> Any:
    """int16 interleaved PCM -> float32 mono at 16 kHz (numpy array)."""
    import numpy as np

    audio = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
    if channels > 1:
        audio = audio[: len(audio) - len(audio) % channels].reshape(-1, channels).mean(axis=1)
    if rate == TARGET_RATE or audio.size == 0:
        return audio
    if rate % TARGET_RATE == 0:
        # Integer ratio (e.g. 48 kHz): averaging each block is a simple low-pass
        # filter plus decimation, which keeps speech clean.
        factor = rate // TARGET_RATE
        audio = audio[: len(audio) - len(audio) % factor]
        return audio.reshape(-1, factor).mean(axis=1)
    n_out = int(round(audio.size * TARGET_RATE / rate))
    x_old = np.linspace(0.0, 1.0, num=audio.size, endpoint=False)
    x_new = np.linspace(0.0, 1.0, num=n_out, endpoint=False)
    return np.interp(x_new, x_old, audio).astype(np.float32)


# ---------------------------------------------------------------- capture ----
class SoundDeviceCapture(AudioCapture):
    """Microphone capture through PortAudio (``sounddevice``), held in memory."""

    name = "sounddevice"

    def __init__(self, default_device: str | None) -> None:
        import sounddevice  # noqa: F401  (fail early if PortAudio is missing)

        self.default_device = default_device
        self._stream: Any = None
        self._queue: asyncio.Queue[AudioFrame | None] = asyncio.Queue()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._channels = 1
        self._rate = TARGET_RATE
        self.active_device: str | None = None

    def _list(self) -> list[dict[str, Any]]:
        import sounddevice as sd

        apis = sd.query_hostapis()
        devices = [dict(d, index=i) for i, d in enumerate(sd.query_devices()) if d["max_input_channels"] > 0]
        names = {apis[d["hostapi"]]["name"] for d in devices}
        api = next((a for a in HOSTAPI_PREFERENCE if a in names), None)
        chosen = [d for d in devices if api is None or apis[d["hostapi"]]["name"] == api]
        return [
            {
                "index": d["index"],
                "name": d["name"],
                "hostapi": apis[d["hostapi"]]["name"],
                "channels": d["max_input_channels"],
                "default_samplerate": d["default_samplerate"],
            }
            for d in chosen
        ]

    async def devices(self) -> list[dict[str, Any]]:
        devs = await asyncio.to_thread(self._list)
        for d in devs:
            d["is_default"] = d["name"] == self.default_device
        return devs

    def _resolve(self, wanted: str | None) -> dict[str, Any]:
        devs = self._list()
        if not devs:
            raise RuntimeError("no input devices found")
        if not wanted:
            import sounddevice as sd

            idx = sd.default.device[0]
            if idx is not None and idx >= 0:
                name = sd.query_devices(idx)["name"]
                match = next((d for d in devs if d["name"] == name or name.startswith(d["name"]) or d["name"].startswith(name)), None)
                if match:
                    return match
            return devs[0]
        exact = [d for d in devs if d["name"] == wanted]
        if exact:
            return exact[0]
        # MME truncates names; also accept a unique prefix/substring match.
        loose = [d for d in devs if wanted.startswith(d["name"]) or d["name"].startswith(wanted) or wanted.lower() in d["name"].lower()]
        if len(loose) == 1:
            return loose[0]
        raise RuntimeError(f"microphone '{wanted}' was not found")

    def _callback(self, indata: Any, frames: int, time_info: Any, status: Any) -> None:
        if status:
            log.debug("audio input status: %s", status)
        frame = AudioFrame(pcm=bytes(indata), sample_rate=self._rate)
        assert self._loop is not None
        self._loop.call_soon_threadsafe(self._queue.put_nowait, frame)

    async def start(self, device: str | None) -> None:
        import sounddevice as sd

        if self._stream is not None:
            raise RuntimeError("microphone already open")
        self._loop = asyncio.get_running_loop()
        self._queue = asyncio.Queue()
        dev = await asyncio.to_thread(self._resolve, device or self.default_device)
        rate = int(dev["default_samplerate"])
        last_exc: Exception | None = None
        for channels in sorted({1, min(2, dev["channels"])}):
            try:
                stream = sd.RawInputStream(
                    samplerate=rate, channels=channels, dtype="int16", device=dev["index"], blocksize=int(rate * 0.05), callback=self._callback
                )
                stream.start()
            except Exception as exc:  # some drivers only accept their native channel count
                last_exc = exc
                continue
            self._stream, self._channels, self._rate = stream, channels, rate
            self.active_device = dev["name"]
            log.info("Microphone opened: %s (%s, %d Hz, %d ch)", dev["name"], dev["hostapi"], rate, channels)
            return
        raise RuntimeError(str(last_exc) if last_exc else "could not open the microphone")

    async def stop(self) -> None:
        stream, self._stream = self._stream, None
        if stream is None:
            return
        try:
            await asyncio.to_thread(stream.stop)
            await asyncio.to_thread(stream.close)
        finally:
            # Scheduled through the loop so the end marker lands after any
            # frames the audio thread queued before the stream stopped.
            assert self._loop is not None
            self._loop.call_soon(self._queue.put_nowait, None)
            log.info("Microphone closed")

    async def frames(self) -> AsyncIterator[AudioFrame]:
        while True:
            frame = await self._queue.get()
            if frame is None:
                return
            yield frame

    @property
    def channels(self) -> int:
        return self._channels

    def describe(self) -> dict[str, Any]:
        return {"device": self.active_device or self.default_device}


# --------------------------------------------------------- speech-to-text ----
def _add_cuda_dll_dirs() -> None:
    """On Windows, make the pip-installed CUDA libraries visible to CTranslate2."""
    if sys.platform != "win32":
        return
    import site

    for base in [*site.getsitepackages(), site.getusersitepackages()]:
        for sub in ("cublas", "cudnn", "cuda_nvrtc"):
            p = Path(base) / "nvidia" / sub / "bin"
            if p.is_dir():
                os.add_dll_directory(str(p))
                os.environ["PATH"] = str(p) + os.pathsep + os.environ.get("PATH", "")


class FasterWhisperSTT(SpeechToText):
    """Whisper through faster-whisper/CTranslate2, on the GPU when available."""

    name = "faster-whisper"
    is_cloud = False

    def __init__(self, model_dir: Path, *, device: str, compute_type: str, languages: tuple[str, ...], channels_of: Any = None) -> None:
        import faster_whisper  # noqa: F401

        self.model_dir = model_dir
        self.languages = languages
        self.requested_device = device
        self.compute_type = compute_type
        self._channels_of = channels_of  # callable giving the capture channel count
        self._model: Any = None
        self.device: str | None = None
        self._load_lock = threading.Lock()

    def _load(self) -> Any:
        with self._load_lock:
            if self._model is not None:
                return self._model
            from faster_whisper import WhisperModel

            device = self.requested_device
            if device in ("auto", "cuda"):
                _add_cuda_dll_dirs()
                import ctranslate2

                has_gpu = ctranslate2.get_cuda_device_count() > 0
                if device == "cuda" and not has_gpu:
                    raise RuntimeError("no CUDA GPU available for speech recognition")
                device = "cuda" if has_gpu else "cpu"
            compute = self.compute_type if device == "cuda" else "int8"
            log.info("Loading Whisper model %s on %s (%s)", self.model_dir.name, device, compute)
            self._model = WhisperModel(str(self.model_dir), device=device, compute_type=compute, local_files_only=True)
            self.device = device
            return self._model

    async def prepare(self) -> None:
        await asyncio.to_thread(self._load)

    def _run(self, pcm: bytes, rate: int, channels: int, language_hint: str | None) -> Transcript:
        model = self._load()
        audio = to_mono_16k(pcm, rate, channels)
        if audio.size < TARGET_RATE * 0.3:  # under 0.3 s: nothing useful was said
            return Transcript(text="", language=None, confidence=None)
        language = language_hint
        confidence = None
        if language is None:
            # Restrict detection to the configured languages (Swedish/English),
            # so a short Swedish phrase is never transcribed as Norwegian.
            _, _, probs = model.detect_language(audio)
            allowed = [(lang, p) for lang, p in probs if lang in self.languages]
            if allowed:
                language, confidence = max(allowed, key=lambda lp: lp[1])
            else:
                language = self.languages[0]
        segments, _info = model.transcribe(audio, language=language, beam_size=5, vad_filter=True, condition_on_previous_text=False)
        text = " ".join(s.text.strip() for s in segments).strip()
        return Transcript(text=text, language=language, confidence=round(confidence, 3) if confidence is not None else None)

    async def transcribe(self, frames: AsyncIterator[AudioFrame], language_hint: str | None = None) -> Transcript:
        chunks: list[bytes] = []
        rate = TARGET_RATE
        async for f in frames:
            chunks.append(f.pcm)
            rate = f.sample_rate
        channels = self._channels_of() if self._channels_of else 1
        return await asyncio.to_thread(self._run, b"".join(chunks), rate, channels, language_hint)

    def describe(self) -> dict[str, Any]:
        return {"model": self.model_dir.name, "device": self.device or f"{self.requested_device} (loads on first use)", "languages": list(self.languages)}


# ----------------------------------------------------------------- playback ----
class SoundDevicePlayback(AudioPlayback):
    name = "sounddevice"

    def __init__(self) -> None:
        import sounddevice  # noqa: F401

    def _play(self, audio: bytes) -> None:
        import numpy as np
        import sounddevice as sd

        with wave.open(io.BytesIO(audio), "rb") as wav:
            rate = wav.getframerate()
            data = np.frombuffer(wav.readframes(wav.getnframes()), dtype=np.int16)
        sd.play(data, rate)
        sd.wait()

    async def play(self, audio: bytes) -> None:
        await asyncio.to_thread(self._play, audio)

    async def interrupt(self) -> None:
        import sounddevice as sd

        sd.stop()


# ----------------------------------------------------------------- wiring ----
def build_voice_controller(cfg: VoiceConfig, models_dir: Path) -> VoiceController:
    """Create the controller with every engine that can run on this machine.

    Missing packages or models leave that stage empty with a reason, so the UI
    shows exactly what is missing instead of pretending to listen.
    """
    vc = VoiceController(max_capture_s=cfg.max_capture_s, languages=tuple(cfg.stt_languages))
    if cfg.enabled is False:
        vc.unavailable["capture"] = vc.unavailable["stt"] = "Voice is disabled in configuration."
        return vc
    try:
        vc.capture = SoundDeviceCapture(cfg.input_device)
    except Exception as exc:  # ImportError, or PortAudio missing
        vc.unavailable["capture"] = f"Microphone engine unavailable ({type(exc).__name__}). Install with: .\\scripts\\setup.ps1 -Voice"
    model_dir = whisper_model_dir(models_dir, cfg.stt_model)
    if not (model_dir / "model.bin").exists():
        vc.unavailable["stt"] = f"Whisper model '{cfg.stt_model}' is not downloaded. Run: python -m jarvis voice-models --download"
    else:
        try:
            capture = vc.capture
            vc.stt = FasterWhisperSTT(
                model_dir,
                device=cfg.stt_device,
                compute_type=cfg.stt_compute_type,
                languages=tuple(cfg.stt_languages),
                channels_of=(lambda: capture.channels) if isinstance(capture, SoundDeviceCapture) else None,
            )
        except Exception as exc:
            vc.unavailable["stt"] = f"Speech recognition unavailable ({type(exc).__name__}). Install with: .\\scripts\\setup.ps1 -Voice"
    try:
        vc.tts = build_tts(cfg, models_dir)
        vc.playback = SoundDevicePlayback()
    except Exception as exc:
        vc.tts = vc.playback = None
        vc.unavailable["tts"] = (
            f"No voice available ({exc}). Run: python -m jarvis voice-models --download"
            if isinstance(exc, (FileNotFoundError, ValueError))
            else f"Speech output unavailable ({type(exc).__name__}). Install with: .\\scripts\\setup.ps1 -Voice"
        )
    return vc


def build_tts(cfg: VoiceConfig, models_dir: Path, *, azure_transport: Any = None) -> VoiceTTS:
    """The configured voice per language, a local fallback for each, and the filter.

    A voice that cannot be created (not downloaded, package missing) is replaced
    by its fallback; a cloud voice without a key still gets created and falls
    back at speaking time, so adding the key later needs only a restart.
    """
    from ..secrets import get_secret

    usage = AzureUsage(models_dir.parent / "voice_usage.json", cfg.azure.monthly_char_limit)
    key, region = get_secret(cfg.azure.key_env), get_secret(cfg.azure.region_env)
    cache: dict[str, Any] = {}

    def backend(spec: str) -> Any:
        if spec not in cache:
            cache[spec] = make_backend(spec, models_dir, cfg.azure, usage, key=key, region=region, transport=azure_transport)
        return cache[spec]

    voices: dict[str, Any] = {}
    fallbacks: dict[str, Any] = {}
    errors: list[str] = []
    for lang, spec, fb in (("sv", cfg.tts_voice_sv, cfg.tts_fallback_sv), ("en", cfg.tts_voice_en, cfg.tts_fallback_en)):
        if not spec:
            continue
        fallback = None
        try:
            fallback = backend(fb)
        except Exception as exc:
            errors.append(f"{fb}: {exc}")
        try:
            voices[lang] = backend(spec)
        except Exception as exc:
            errors.append(f"{spec}: {exc}")
            if fallback is not None:
                voices[lang] = fallback
        if fallback is not None and lang in voices:
            fallbacks[lang] = fallback
    if errors:
        log.warning("Some voices are unavailable: %s", "; ".join(errors))
    if not voices:
        raise FileNotFoundError("; ".join(errors) or "no voice configured")
    effects = None
    if cfg.effects.enabled:
        try:
            from .effects import JarvisFilter

            effects = JarvisFilter(cfg.effects)
        except Exception as exc:
            log.warning("JARVIS filter unavailable (%s); speaking without it", exc)
    return VoiceTTS(voices, fallbacks, effects)
