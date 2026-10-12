from conftest import ScriptedProvider, make_config

from jarvis.computer import windows
from jarvis.toolcheck import CASES, Case, format_report, run_toolcheck


async def test_toolcheck_scores_tool_use_and_forces_dry_run(tmp_path, monkeypatch):
    monkeypatch.setattr(windows, "IS_WINDOWS", True)
    launched = []
    monkeypatch.setattr(windows.subprocess, "Popen", lambda *a, **k: launched.append(a))
    cases = [
        Case("Öppna anteckningar", frozenset({"computer_open_app"})),
        Case("Vilka program får du starta?", frozenset({"computer_list_apps"})),
        Case("Berätta ett kort skämt", frozenset()),
    ]
    ollama = ScriptedProvider(
        "ollama",
        "local",
        [
            [("computer_open_app", {"app": "notepad"})], "Simulated.",  # uses the tool
            "Notepad, Paint, Word",  # answers from memory: must fail
            "Ett skämt.",  # no tool expected
        ],
    )
    cfg = make_config(tmp_path, computer={"enabled": None, "dry_run": False}, homeassistant={"dry_run": False})
    results = await run_toolcheck(cfg, providers={"ollama": ollama}, cases=cases)

    assert [r.passed for r in results] == [True, False, True]
    assert results[0].called == ["computer_open_app"]
    assert launched == []  # dry-run was forced on despite the config
    assert all(req["model"] is None for req in ollama.requests)
    report = format_report(results, "qwen2.5:7b")
    assert "Result: 2/3 passed" in report and "[FAIL]" in report and "Paint" in report


async def test_toolcheck_skips_unavailable_tools(tmp_path, monkeypatch):
    monkeypatch.setattr(windows, "IS_WINDOWS", False)  # e.g. Raspberry Pi: no computer tools
    ollama = ScriptedProvider("ollama", "local", [])
    results = await run_toolcheck(make_config(tmp_path, computer={"enabled": None}), providers={"ollama": ollama})
    prompts = {r.case.prompt for r in results}
    assert "Öppna anteckningar" not in prompts and "Vad är klockan?" in prompts
    assert len(CASES) > len(results)
