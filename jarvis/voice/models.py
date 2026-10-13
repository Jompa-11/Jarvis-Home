"""Where voice models live, and the explicit download step.

JARVIS never downloads a model on its own while running. ``python -m jarvis
voice-models`` shows what is present; ``--download`` fetches what is missing
from Hugging Face into ``data/models`` (git-ignored).
"""

from __future__ import annotations

import shutil
from pathlib import Path

# Whisper sizes JARVIS knows how to fetch (CTranslate2 conversions).
WHISPER_REPOS = {
    "small": "Systran/faster-whisper-small",
    "medium": "Systran/faster-whisper-medium",
    "large-v3": "Systran/faster-whisper-large-v3",
    "large-v3-turbo": "mobiuslabsgmbh/faster-whisper-large-v3-turbo",
}
PIPER_REPO = "rhasspy/piper-voices"


def whisper_model_dir(models_dir: Path, model: str) -> Path:
    p = Path(model)
    return p if p.is_absolute() else models_dir / "whisper" / model


def piper_voice_path(models_dir: Path, voice: str) -> Path:
    p = Path(voice)
    if p.is_absolute():
        return p
    return models_dir / "piper" / f"{voice}.onnx"


def piper_repo_path(voice: str) -> str:
    """'sv_SE-nst-medium' -> 'sv/sv_SE/nst/medium/sv_SE-nst-medium'."""
    locale, name, quality = voice.split("-", 2)
    return f"{locale.split('_')[0]}/{locale}/{name}/{quality}/{voice}"


def status(models_dir: Path, whisper: str, voices: list[str]) -> list[dict]:
    rows = [{"kind": "whisper", "name": whisper, "path": str(whisper_model_dir(models_dir, whisper)), "present": (whisper_model_dir(models_dir, whisper) / "model.bin").exists()}]
    for v in voices:
        p = piper_voice_path(models_dir, v)
        rows.append({"kind": "piper", "name": v, "path": str(p), "present": p.exists() and p.with_suffix(".onnx.json").exists()})
    return rows


def download(models_dir: Path, whisper: str, voices: list[str]) -> None:  # pragma: no cover - network
    from huggingface_hub import hf_hub_download, snapshot_download

    target = whisper_model_dir(models_dir, whisper)
    if not (target / "model.bin").exists():
        repo = WHISPER_REPOS.get(whisper)
        if repo is None:
            raise SystemExit(f"Unknown Whisper model '{whisper}'. Known: {', '.join(WHISPER_REPOS)}")
        print(f"Downloading Whisper '{whisper}' from {repo} ...")
        snapshot_download(repo, local_dir=target, allow_patterns=["*.json", "*.bin", "*.txt"])
    for v in voices:
        p = piper_voice_path(models_dir, v)
        if p.exists() and p.with_suffix(".onnx.json").exists():
            continue
        print(f"Downloading Piper voice '{v}' ...")
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.parent / "_download"
        for suffix in (".onnx", ".onnx.json"):
            src = hf_hub_download(PIPER_REPO, piper_repo_path(v) + suffix, local_dir=tmp)
            Path(src).replace(p.parent / f"{v}{suffix}")
        shutil.rmtree(tmp, ignore_errors=True)
