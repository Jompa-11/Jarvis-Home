"""The optional "JARVIS filter": an effect chain applied after speech synthesis.

Synthetic voices from any engine pass through the same chain, so the
character comes from processing and not from imitating anyone:

1. Slightly lower pitch (tempo unchanged).
2. EQ: remove rumble, add clarity/presence around 3 kHz and a little air.
3. Subtle "digital sheen": a light, slow chorus mixed in quietly.
4. A short, soft reverb, as if speaking in a small, well-damped room.
5. Gentle compression and a limiter so nothing clips.

Audio is processed in memory (WAV bytes in, WAV bytes out). Uses Spotify's
``pedalboard`` (imported lazily).
"""

from __future__ import annotations

import io
import wave
from typing import Any

from ..config import VoiceEffectsConfig


def _read_wav(data: bytes) -> tuple[Any, int]:
    import numpy as np

    with wave.open(io.BytesIO(data), "rb") as w:
        rate, channels = w.getframerate(), w.getnchannels()
        pcm = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32) / 32768.0
    if channels > 1:
        pcm = pcm.reshape(-1, channels).mean(axis=1)
    return pcm, rate


def _write_wav(audio: Any, rate: int) -> bytes:
    import numpy as np

    pcm = (np.clip(audio, -1.0, 1.0) * 32767.0).astype(np.int16)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm.tobytes())
    return buf.getvalue()


def build_chain(cfg: VoiceEffectsConfig) -> Any:
    from pedalboard import (
        Chorus,
        Compressor,
        HighpassFilter,
        HighShelfFilter,
        Limiter,
        PeakFilter,
        Pedalboard,
        PitchShift,
        Reverb,
    )

    plugins: list[Any] = []
    if cfg.pitch_semitones:
        plugins.append(PitchShift(semitones=cfg.pitch_semitones))
    plugins.append(HighpassFilter(cutoff_frequency_hz=cfg.highpass_hz))
    if cfg.warmth_db:
        plugins.append(PeakFilter(cutoff_frequency_hz=180.0, gain_db=cfg.warmth_db, q=0.8))
    if cfg.presence_db:
        plugins.append(PeakFilter(cutoff_frequency_hz=3000.0, gain_db=cfg.presence_db, q=1.0))
    if cfg.air_db:
        plugins.append(HighShelfFilter(cutoff_frequency_hz=8000.0, gain_db=cfg.air_db))
    if cfg.sheen_mix:
        plugins.append(Chorus(rate_hz=0.5, depth=0.15, centre_delay_ms=6.0, feedback=0.0, mix=cfg.sheen_mix))
    if cfg.reverb_wet:
        plugins.append(Reverb(room_size=cfg.reverb_room_size, damping=0.7, wet_level=cfg.reverb_wet, dry_level=1.0 - cfg.reverb_wet / 2, width=0.5))
    plugins.append(Compressor(threshold_db=-20.0, ratio=2.5, attack_ms=5.0, release_ms=80.0))
    plugins.append(Limiter(threshold_db=-1.5))
    return Pedalboard(plugins)


class JarvisFilter:
    """Applies the effect chain to WAV bytes."""

    def __init__(self, cfg: VoiceEffectsConfig) -> None:
        self.cfg = cfg
        self._chain = build_chain(cfg)

    def __call__(self, wav: bytes) -> bytes:
        import numpy as np

        audio, rate = _read_wav(wav)
        if audio.size == 0:
            return wav
        # Pad the end so the reverb tail is not cut off.
        tail = np.zeros(int(rate * 0.35), dtype=np.float32)
        out = self._chain(np.concatenate([audio, tail]), rate)
        out = out[0] if out.ndim > 1 else out
        return _write_wav(out * self.cfg.output_gain, rate)

    def describe(self) -> dict[str, Any]:
        return self.cfg.model_dump(exclude={"enabled"})
