"""Voice pipeline interfaces (Phase 2 implements them).

The pipeline is split into independent, swappable stages so local and cloud
engines can be compared on cost, latency and Swedish accuracy::

    AudioCapture -> WakeWordDetector -> SpeechToText -> Orchestrator
                                                          |
                         AudioPlayback <- TextToSpeech <--+

Every stage can be cancelled. ``VoiceController`` owns the microphone state and
is the single source of truth the UI displays. Phase 1 ships no capture
engine, so the controller reports ``not_implemented`` and never claims to
listen.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass
from enum import Enum
from typing import Any


class MicState(str, Enum):
    NOT_IMPLEMENTED = "not_implemented"  # no capture engine installed
    DISABLED = "disabled"  # master switch off
    IDLE = "idle"  # engine ready, not capturing
    WAKE_LISTENING = "wake_listening"  # listening only for the wake word, locally
    CAPTURING = "capturing"  # recording an utterance
    ERROR = "error"


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
    @abstractmethod
    async def devices(self) -> list[dict[str, Any]]: ...

    @abstractmethod
    def frames(self) -> AsyncIterator[AudioFrame]: ...

    @abstractmethod
    async def stop(self) -> None: ...


class WakeWordDetector(ABC):
    @abstractmethod
    async def detect(self, frames: AsyncIterator[AudioFrame]) -> bool:
        """Return True when the wake word is heard. Must run fully locally."""


class SpeechToText(ABC):
    name: str
    is_cloud: bool
    languages: tuple[str, ...]

    @abstractmethod
    async def transcribe(self, frames: AsyncIterator[AudioFrame], language_hint: str | None = None) -> Transcript: ...


class TextToSpeech(ABC):
    name: str
    is_cloud: bool
    languages: tuple[str, ...]

    @abstractmethod
    async def synthesize(self, text: str, language: str) -> bytes: ...


class AudioPlayback(ABC):
    @abstractmethod
    async def play(self, audio: bytes) -> None: ...

    @abstractmethod
    async def interrupt(self) -> None:
        """Stop playback immediately (barge-in)."""


class VoiceController:
    """Owns microphone state. Phase 1: no engines registered."""

    def __init__(self) -> None:
        self.capture: AudioCapture | None = None
        self.wake_word: WakeWordDetector | None = None
        self.stt: SpeechToText | None = None
        self.tts: TextToSpeech | None = None
        self.playback: AudioPlayback | None = None
        self.enabled = False

    @property
    def implemented(self) -> bool:
        return all([self.capture, self.stt])

    def set_enabled(self, enabled: bool) -> None:
        self.enabled = enabled

    @property
    def mic_state(self) -> MicState:
        if not self.implemented:
            return MicState.NOT_IMPLEMENTED
        if not self.enabled:
            return MicState.DISABLED
        return MicState.IDLE

    def status(self) -> dict[str, Any]:
        def stage(obj: Any) -> dict[str, Any]:
            if obj is None:
                return {"state": "not_implemented"}
            return {"state": "ready", "name": getattr(obj, "name", type(obj).__name__), "cloud": getattr(obj, "is_cloud", False)}

        return {
            "mic_state": self.mic_state.value,
            "master_switch": "on" if self.enabled else "off",
            "capture": stage(self.capture),
            "wake_word": stage(self.wake_word),
            "stt": stage(self.stt),
            "tts": stage(self.tts),
            "playback": stage(self.playback),
        }
