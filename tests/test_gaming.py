"""Game mode: detection from a faked process list and GPU value, and its effects."""

from __future__ import annotations

from conftest import ScriptedProvider
from fake_mail import FakeMailProvider, mail

from jarvis.config import GamingConfig
from jarvis.monitoring.gaming import GamingDetector, GamingMode
from jarvis.rules import RulesConfig


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


def detector(procs=(), gpu=None, **cfg):
    state = {"procs": set(procs), "gpu": gpu}
    clock = Clock()
    d = GamingDetector(GamingConfig(**cfg), processes=lambda: state["procs"], gpu=lambda: state["gpu"], clock=clock)
    return d, state, clock


def test_listed_game_process_turns_game_mode_on():
    d, state, _ = detector(["explorer.exe", "CS2.EXE"])
    assert d.check() == "cs2.exe"
    state["procs"] = {"explorer.exe"}
    assert d.check() is None
    d2, _, _ = detector(["valorant.exe"], process_names=["cs2.exe", "valorant.exe"])
    assert d2.check() == "valorant.exe"  # easy to add more games


def test_busy_gpu_needs_two_minutes():
    d, state, clock = detector(gpu=97)
    assert d.check() is None
    clock.t += 119
    assert d.check() is None
    clock.t += 1
    assert d.check() == "GPU 97 % i 2 min"
    state["gpu"] = 40  # a dip resets the timer
    assert d.check() is None
    state["gpu"] = 90
    clock.t += 60
    assert d.check() is None


def test_gpu_at_threshold_or_unknown_is_not_gaming():
    d, state, clock = detector(gpu=85)
    clock.t += 600
    assert d.check() is None
    state["gpu"] = None  # no nvidia-smi
    assert d.check() is None


def test_disabled_and_check_now():
    d, _, _ = detector(["cs2.exe"], gpu=99, enabled=False)
    assert d.check() is None and d.check_now() is None
    d2, state, _ = detector(gpu=90)
    assert d2.check_now() == "GPU 90 %"  # toolcheck does not wait two minutes
    state["gpu"] = 10
    state["procs"] = {"cs2.exe"}
    assert d2.check_now() == "cs2.exe"


async def test_mode_transitions_call_start_and_end_once():
    d, state, _ = detector()
    calls = []

    async def start():
        calls.append("start")

    async def end():
        calls.append("end")

    mode = GamingMode(d, on_start=start, on_end=end)
    assert await mode.poll() is False
    state["procs"] = {"cs2.exe"}
    assert await mode.poll() is True and await mode.poll() is True
    assert mode.status()["active"] and mode.status()["reason"] == "cs2.exe" and mode.since
    state["procs"] = set()
    assert await mode.poll() is False
    assert calls == ["start", "end"]


class FakeOllama(ScriptedProvider):
    def __init__(self) -> None:
        super().__init__("ollama", "local", ['{"priority": "normal", "reason": "vanligt", "summary": "Ett vanligt mejl."}'])
        self.unloads = 0

    async def unload(self) -> bool:
        self.unloads += 1
        return True


async def test_game_mode_defers_model_calls_but_not_rule_matches(make_core):
    rules = RulesConfig.model_validate({"mail": {"important_domains": ["skatteverket.se"]}})
    provider = FakeMailProvider([
        mail("m1", "Skatteverket <info@skatteverket.se>", "Deklarationen är klar"),
        mail("m2", "Anna <anna@example.se>", "Fika?"),
    ])
    ollama = FakeOllama()
    core = make_core({"ollama": ollama}, rules=rules, mail_provider=provider, gaming={"enabled": True}, ai={"routes": {"conversation": [{"provider": "ollama"}]}})
    procs: set[str] = {"cs2.exe"}
    core.gaming.detector.processes = lambda: procs
    core.gaming.detector.gpu = lambda: None

    await core.gaming.poll()
    assert core.gaming.active and ollama.unloads == 1  # the model is unloaded from the GPU
    await core.mail.poll()
    titles = [n["title"] for n in core.monitor.notifier.list_recent()]
    assert titles == ["Viktigt mejl från Skatteverket (skatteverket.se)"]  # rule match: notified at once
    assert ollama.requests == [] and list(core.mail.waiting) == ["m2"]  # no model call while gaming
    await core.mail.poll()
    assert ollama.requests == [] and list(core.mail.waiting) == ["m2"]  # not queued twice
    assert core.monitor.snapshot()["gaming"]["active"] is True

    procs.clear()  # the game closes
    await core.gaming.poll()
    assert not core.gaming.active and core.mail.waiting == {}
    assert len(ollama.requests) == 1 and ollama.requests[0]["tools"] == []  # classified now, still without tools
    assert len(core.monitor.notifier.list_recent()) == 2
    assert core.monitor.snapshot()["gaming"]["active"] is False


def test_toolcheck_refuses_during_game_mode(monkeypatch, tmp_path, capsys):
    from jarvis import __main__ as cli
    from jarvis.monitoring import gaming

    monkeypatch.setattr(gaming.GamingDetector, "check_now", lambda self: "cs2.exe")
    monkeypatch.setattr(cli, "load_dotenv", lambda *a, **k: None, raising=False)
    assert cli.main(["toolcheck", "--config", str(tmp_path / "none.toml")]) == 3
    assert "Game mode is on (cs2.exe)" in capsys.readouterr().out
