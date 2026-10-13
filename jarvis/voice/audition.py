"""Play candidate voices on this computer's speakers, without and with the filter.

    python -m jarvis voice-audition --lang en --voices "piper:en_GB-alan-medium,kokoro:bm_george"
    python -m jarvis voice-audition --lang sv --effect pitch_semitones=-2 --only filtered

Each candidate first announces itself (in its own voice), then reads the test
sentence without the filter and with it. Nothing is recorded or saved.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from ..config import VoiceConfig, VoiceEffectsConfig
from .tts import AzureUsage, make_backend, parse_voice

SENTENCES = {
    "en": "Good morning, sir. It's half past nine and five degrees outside. I've taken the liberty of preparing your schedule.",
    "sv": "God morgon, sir. Klockan är halv tio och det är fem grader ute. Jag har tagit mig friheten att förbereda dagens schema.",
}
_ANNOUNCE = {
    "en": ("Voice {n}: {name}.", "Without filter.", "With filter."),
    "sv": ("Röst {n}: {name}.", "Utan filter.", "Med filter."),
}


def spoken_name(spec: str) -> str:
    vs = parse_voice(spec)
    name = vs.name
    for prefix in ("en_GB-", "sv_SE-", "en-GB-", "sv-SE-"):
        name = name.removeprefix(prefix)
    name = name.replace("-medium", "").replace("-high", "").replace("-low", "").replace("Neural", "")
    name = name.replace("_", " ").replace("-", " ")
    if vs.engine == "kokoro":
        name = name.removeprefix("bm ").removeprefix("bf ")
    speaker = f", speaker {vs.speaker}" if vs.speaker else ""
    return f"{vs.engine.capitalize()} {name}{speaker}"


async def audition(cfg: VoiceConfig, models_dir: Path, lang: str, voices: list[str], *, effects: VoiceEffectsConfig, only: str | None = None, sentence: str | None = None) -> None:  # pragma: no cover - plays audio
    from ..secrets import get_secret
    from .effects import JarvisFilter
    from .engines import SoundDevicePlayback

    usage = AzureUsage(models_dir.parent / "voice_usage.json", cfg.azure.monthly_char_limit)
    key, region = get_secret(cfg.azure.key_env), get_secret(cfg.azure.region_env)
    jfilter = JarvisFilter(effects)
    player = SoundDevicePlayback()
    text = sentence or SENTENCES[lang]
    intro, plain, filtered = _ANNOUNCE[lang]
    for n, spec in enumerate(voices, 1):
        try:
            backend = make_backend(spec, models_dir, cfg.azure, usage, key=key, region=region)
            print(f"[{n}] {spec}", flush=True)
            await player.play(await asyncio.to_thread(backend.synth, intro.format(n=n, name=spoken_name(spec))))
            if only != "filtered":
                print("    without filter", flush=True)
                await player.play(await asyncio.to_thread(backend.synth, plain))
                await player.play(await asyncio.to_thread(backend.synth, text))
            if only != "plain":
                print("    with filter", flush=True)
                await player.play(await asyncio.to_thread(backend.synth, filtered))
                await player.play(jfilter(await asyncio.to_thread(backend.synth, text)))
        except Exception as exc:
            print(f"[{n}] {spec}: could not play ({exc})", flush=True)
        await asyncio.sleep(0.6)
