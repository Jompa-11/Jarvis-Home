"""Voice pipeline interfaces and the controller that owns the microphone.

The pipeline is split into independent, swappable stages so local and cloud
engines can be compared on cost, latency and Swedish accuracy::

    AudioCapture -> (WakeWordDetector) -> SpeechToText -> Orchestrator
                                                              |
                             AudioPlayback <- TextToSpeech <--+

Phase 2 is push-to-talk: there is no wake word and no always-on listening.
``VoiceController`` opens the microphone only while BOTH the master switch is
on AND the user holds the microphone button, and it is the single source of
truth the UI displays. Audio stays in memory; nothing is written to disk.
"""

from __future__ import annotations

import asyncio
import logging
import time
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from enum import Enum
from typing import Any

log = logging.getLogger(__name__)


class MicState(str, Enum):
    NOT_IMPLEMENTED = "not_implemented"  # no capture or speech-to-text engine installed
    DISABLED = "disabled"  # master switch off
    IDLE = "idle"  # engine ready, microphone closed
    WAKE_LISTENING = "wake_listening"  # listening only for the wake word, locally (not used yet)
    CAPTURING = "capturing"  # microphone open: the user is holding the button
    PROCESSING = "processing"  # microphone closed, turning speech into text
    ERROR = "error"


class VoiceError(Exception):
    """A voice request that cannot be carried out (shown to the user)."""


@dataclass
class AudioFrame:
    pcm: bytes  # 16-bit little-endian mono
    sample_rate: int = 16000


@dataclass
class Transcript:
    text: str
    language: str | None  # "sv", "en", ...
    confidence: float | None = None
    is_final: bool = True


class AudioCapture(ABC):
    name: str

    @abstractmethod
    async def devices(self) -> list[dict[str, Any]]:
        """Input devices the user may choose from."""

    @abstractmethod
    async def start(self, device: str | None) -> None:
        """Open the microphone and start buffering frames in memory."""

    @abstractmethod
    def frames(self) -> AsyncIterator[AudioFrame]:
        """Yield the buffered frames; ends after ``stop()``."""

    @abstractmethod
    async def stop(self) -> None:
        """Close the microphone. Safe to call more than once."""


class WakeWordDetector(ABC):
    @abstractmethod
    async def detect(self, frames: AsyncIterator[AudioFrame]) -> bool:
        """Return True when the wake word is heard. Must run fully locally."""


class SpeechToText(ABC):
    name: str
    is_cloud: bool
    languages: tuple[str, ...]

    async def prepare(self) -> None:
        """Load the model ahead of the first request (optional)."""

    @abstractmethod
    async def transcribe(self, frames: AsyncIterator[AudioFrame], language_hint: str | None = None) -> Transcript: ...


class TextToSpeech(ABC):
    name: str
    is_cloud: bool
    languages: tuple[str, ...]

    @abstractmethod
    async def synthesize(self, text: str, language: str) -> bytes:
        """Return a WAV file as bytes, built in memory."""


class AudioPlayback(ABC):
    @abstractmethod
    async def play(self, audio: bytes) -> None: ...

    @abstractmethod
    async def interrupt(self) -> None:
        """Stop playback immediately (barge-in)."""


class VoiceController:
    """Owns microphone state and runs push-to-talk turns.

    The microphone opens in ``begin_capture`` and closes in ``end_capture``,
    ``cancel_capture``, when the master switch is turned off, or when
    ``max_capture_s`` is reached, whichever comes first.
    """

    def __init__(self, *, max_capture_s: float = 30.0, languages: tuple[str, ...] = ("sv", "en")) -> None:
        self.capture: AudioCapture | None = None
        self.wake_word: WakeWordDetector | None = None
        self.stt: SpeechToText | None = None
        self.tts: TextToSpeech | None = None
        self.playback: AudioPlayback | None = None
        self.enabled = False
        self.max_capture_s = max_capture_s
        self.languages = languages
        # Why an engine is missing (e.g. "Voice packages are not installed").
        self.unavailable: dict[str, str] = {}
        self.last_error: str | None = None
        self.speaking = False
        self._speech_gen = 0  # bumped on every interrupt; a stale reading stops
        self.on_change: Callable[[], None] | None = None
        self._capturing = False
        self._processing = False
        self._capture_started = 0.0
        self._watchdog: asyncio.Task | None = None
        self._tasks: set[asyncio.Future] = set()
        self._lock = asyncio.Lock()

    # -- state ---------------------------------------------------------------
    @property
    def implemented(self) -> bool:
        return all([self.capture, self.stt])

    @property
    def capturing(self) -> bool:
        return self._capturing

    @property
    def mic_state(self) -> MicState:
        if not self.implemented:
            return MicState.NOT_IMPLEMENTED
        if not self.enabled:
            return MicState.DISABLED
        if self._capturing:
            return MicState.CAPTURING
        if self._processing:
            return MicState.PROCESSING
        return MicState.IDLE

    def _changed(self) -> None:
        if self.on_change:
            try:
                self.on_change()
            except Exception:
                log.exception("voice state listener failed")

    def set_enabled(self, enabled: bool) -> None:
        self.enabled = enabled
        if not enabled and self._capturing:
            # Turning the master switch off closes the microphone at once and
            # discards what was recorded.
            self._keep(asyncio.ensure_future(self.cancel_capture("Microphone master switch turned off.")))
        self._changed()

    def _keep(self, task: asyncio.Future) -> None:
        # Hold a reference so a background task is not garbage-collected mid-run.
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def status(self) -> dict[str, Any]:
        def stage(key: str, obj: Any) -> dict[str, Any]:
            if obj is None:
                out: dict[str, Any] = {"state": "not_implemented"}
                if key in self.unavailable:
                    out["detail"] = self.unavailable[key]
                return out
            out = {"state": "ready", "name": getattr(obj, "name", type(obj).__name__), "cloud": getattr(obj, "is_cloud", False)}
            describe = getattr(obj, "describe", None)
            if callable(describe):
                out.update(describe())
            return out

        return {
            "mic_state": self.mic_state.value,
            "master_switch": "on" if self.enabled else "off",
            "mode": "push_to_talk",
            "speaking": self.speaking,
            "last_error": self.last_error,
            "max_capture_s": self.max_capture_s,
            "capture": stage("capture", self.capture),
            "wake_word": stage("wake_word", self.wake_word),
            "stt": stage("stt", self.stt),
            "tts": stage("tts", self.tts),
            "playback": stage("playback", self.playback),
        }

    # -- push-to-talk ----------------------------------------------------------
    async def devices(self) -> list[dict[str, Any]]:
        if not self.capture:
            return []
        return await self.capture.devices()

    async def begin_capture(self, device: str | None = None) -> None:
        """Open the microphone. Only allowed with the master switch on."""
        async with self._lock:
            if not self.implemented:
                raise VoiceError(self.unavailable.get("stt") or self.unavailable.get("capture") or "No voice engine is installed.")
            if not self.enabled:
                raise VoiceError("The microphone master switch is off.")
            if self._capturing:
                raise VoiceError("Already listening.")
            if self._processing:
                raise VoiceError("Still processing the previous recording.")
            assert self.capture is not None and self.stt is not None
            if self.speaking:
                await self.interrupt_speech()  # barge-in: stop talking when the user talks
            self.last_error = None
            try:
                await self.capture.start(device)
            except Exception as exc:
                self.last_error = f"Could not open the microphone: {exc}"
                self._changed()
                raise VoiceError(self.last_error) from exc
            self._capturing = True
            self._capture_started = time.monotonic()
            self._watchdog = asyncio.create_task(self._limit_capture(), name="jarvis-mic-limit")
            # Load the speech model while the user is still talking.
            self._keep(asyncio.create_task(self._prepare_stt(), name="jarvis-stt-prepare"))
        self._changed()

    async def end_capture(self) -> tuple[Transcript, float]:
        """Close the microphone and transcribe. Returns (transcript, seconds of audio)."""
        async with self._lock:
            if not self._capturing:
                raise VoiceError(self.last_error or "Not listening.")
            assert self.capture is not None and self.stt is not None
            duration = time.monotonic() - self._capture_started
            await self._close_mic()
            self._processing = True
        self._changed()
        try:
            transcript = await self.stt.transcribe(self.capture.frames())
        except Exception as exc:
            self.last_error = f"Speech recognition failed: {exc}"
            log.exception("Speech recognition failed")
            raise VoiceError(self.last_error) from exc
        finally:
            self._processing = False
            self._changed()
        return transcript, round(duration, 2)

    async def cancel_capture(self, reason: str | None = None) -> bool:
        """Close the microphone and discard the recording."""
        async with self._lock:
            if not self._capturing:
                return False
            assert self.capture is not None
            await self._close_mic()
            async for _ in self.capture.frames():  # drop buffered audio
                pass
            if reason:
                self.last_error = reason
        self._changed()
        return True

    async def _close_mic(self) -> None:
        self._capturing = False
        if self._watchdog and self._watchdog is not asyncio.current_task():
            self._watchdog.cancel()
        self._watchdog = None
        assert self.capture is not None
        try:
            await self.capture.stop()
        except Exception:
            log.exception("Closing the microphone failed")

    async def _limit_capture(self) -> None:
        await asyncio.sleep(self.max_capture_s)
        log.info("Push-to-talk reached %.0f s; closing the microphone", self.max_capture_s)
        await self.cancel_capture(f"Recording stopped after {self.max_capture_s:.0f} seconds (the limit).")

    async def _prepare_stt(self) -> None:
        try:
            assert self.stt is not None
            await self.stt.prepare()
        except Exception:
            log.exception("Loading the speech model failed")

    # -- speech output -----------------------------------------------------------
    async def speak(self, text: str, language: str | None = None) -> bool:
        """Read ``text`` aloud on this computer's speakers.

        The text is cleaned for speech first (no markdown, lists, emojis or
        code) and played sentence by sentence, so it starts quickly and
        ``interrupt_speech`` stops it between and within sentences. Returns
        False when there is no speech engine, nothing to say, or it was
        interrupted.
        """
        from .speech_text import clean_for_speech, guess_language, split_sentences

        if not self.tts or not self.playback:
            return False
        spoken = clean_for_speech(text)
        if not spoken:
            return False
        lang = language or guess_language(spoken)
        lang = lang if lang in self.tts.languages else self.tts.languages[0]
        if self.speaking:
            await self.interrupt_speech()  # a new reply replaces the one being read
        self._speech_gen += 1
        gen = self._speech_gen
        self.speaking = True
        self._changed()
        try:
            for sentence in split_sentences(spoken):
                audio = await self.tts.synthesize(sentence, lang)
                if gen != self._speech_gen:
                    return False
                await self.playback.play(audio)
                if gen != self._speech_gen:
                    return False
        except Exception as exc:
            self.last_error = f"Speech output failed: {exc}"
            log.exception("Speech output failed")
            return False
        finally:
            if gen == self._speech_gen and self.speaking:
                self.speaking = False
                self._changed()
        return True

    async def interrupt_speech(self) -> bool:
        """Stop reading aloud immediately. Returns True if something was playing."""
        was = self.speaking
        self._speech_gen += 1
        self.speaking = False
        if self.playback:
            try:
                await self.playback.interrupt()
            except Exception:
                log.exception("Stopping playback failed")
        if was:
            self._changed()
        return was

    async def shutdown(self) -> None:
        await self.cancel_capture()
        if self.speaking:
            await self.interrupt_speech()
