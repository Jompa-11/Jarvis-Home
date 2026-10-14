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


def test_reply_has_time_accepts_only_the_correct_time():
    from datetime import datetime

    from jarvis.toolcheck import reply_has_time

    start, end = datetime(2026, 10, 14, 12, 22, 30), datetime(2026, 10, 14, 12, 22, 50)
    assert reply_has_time("Klockan är 12:22.", start, end)
    assert reply_has_time("Den är 12.23 nu", start, end)  # +1 min
    assert reply_has_time("Klockan är 12:21", start, end)  # -1 min
    assert not reply_has_time("Klockan är 12:25", start, end)
    assert not reply_has_time("Klockan är 13:22", start, end)
    assert not reply_has_time("Jag vet inte.", start, end)
    assert not reply_has_time("Kod 112:22", start, end)
    midnight = datetime(2026, 10, 14, 23, 59, 40), datetime(2026, 10, 15, 0, 0, 5)
    assert reply_has_time("00:00", *midnight) and reply_has_time("23:59", *midnight)


async def test_clock_case_passes_with_correct_time_from_context(tmp_path):
    from datetime import datetime

    def answer(messages):
        return f"Klockan är {datetime.now():%H:%M}."

    ollama = ScriptedProvider("ollama", "local", [answer, "Klockan är 03:07."])
    case = next(c for c in CASES if c.prompt == "Vad är klockan?")
    results = await run_toolcheck(make_config(tmp_path), providers={"ollama": ollama}, cases=[case, case])
    assert results[0].passed and results[0].via_context and results[0].called == []
    assert "correct time from context" in format_report(results, "m")
    # A wrong time does not pass (unless it happens to be 03:07 right now).
    assert results[1].passed is (datetime.now().strftime("%H:%M") in {"03:06", "03:07", "03:08"})
