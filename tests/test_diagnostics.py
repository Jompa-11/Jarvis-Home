import subprocess

from jarvis import diagnostics


def test_nvidia_smi_overrides_capped_windows_vram(monkeypatch):
    monkeypatch.setattr(diagnostics.shutil, "which", lambda name: "nvidia-smi" if name == "nvidia-smi" else None)
    monkeypatch.setattr(
        diagnostics.subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout="NVIDIA GeForce RTX 4060, 8188\n", stderr=""),
    )
    wmi = [{"name": "NVIDIA GeForce RTX 4060", "vram_mb": 4095}, {"name": "Intel UHD", "vram_mb": 1024}]
    gpus = diagnostics._merge_gpus(wmi, diagnostics._nvidia_gpus())
    assert gpus[0] == {"name": "NVIDIA GeForce RTX 4060", "vram_mb": 8188, "source": "nvidia-smi"}
    assert gpus[1]["source"].startswith("windows")


def test_no_nvidia_driver(monkeypatch):
    monkeypatch.setattr(diagnostics.shutil, "which", lambda name: None)
    assert diagnostics._nvidia_gpus() == []
