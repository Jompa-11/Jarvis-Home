"""Measure whether a model uses JARVIS's tools instead of answering from memory.

    python -m jarvis toolcheck                      # the configured Ollama model
    python -m jarvis toolcheck --model llama3.1:8b  # compare another local model
    python -m jarvis toolcheck --spoken             # as spoken requests (replies read aloud);
                                                    # also checks the reply language

Every prompt runs through the real orchestrator with dry-run forced on for
both the computer and the smart home, so nothing is opened or changed.
Only the local Ollama provider is used, so the check costs nothing.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from .app_core import JarvisCore
from .config import JarvisConfig, ModelRoute
from .providers.base import AIProvider
from .voice.speech_text import clean_for_speech, guess_language


@dataclass(frozen=True)
class Case:
    prompt: str
    expected: frozenset[str]  # tools that must be called; empty = no tool expected
    language: str = "sv"  # language the reply should be in (checked for spoken requests)


CASES: list[Case] = [
    Case("Vilka program får du starta?", frozenset({"computer_list_apps"})),
    Case("Öppna anteckningar", frozenset({"computer_open_app"})),
    Case("Starta miniräknaren", frozenset({"computer_open_app"})),
    Case("Vad är klockan?", frozenset({"system_get_time"})),
    Case("Visa systemstatus", frozenset({"system_get_status"})),
    Case("Which speakers do you have?", frozenset({"sonos_list_speakers"}), "en"),
    Case("Pausa musiken i köket", frozenset({"sonos_playback"})),
    Case("Berätta ett kort skämt", frozenset()),
]


@dataclass
class CaseResult:
    case: Case
    called: list[str]
    passed: bool
    seconds: float
    reply: str
    error: str | None = None


def _isolated_config(cfg: JarvisConfig, model: str | None) -> JarvisConfig:
    test_cfg = cfg.model_copy(deep=True)
    route = [ModelRoute(provider="ollama", model=model)]
    test_cfg.ai.routes = {name: [r.model_copy() for r in route] for name in cfg.ai.routes}
    test_cfg.ai.allow_dev_fallback = False
    test_cfg.ai.max_tool_rounds = 4
    return test_cfg


async def run_toolcheck(
    cfg: JarvisConfig,
    *,
    model: str | None = None,
    providers: dict[str, AIProvider] | None = None,
    cases: list[Case] | None = None,
    spoken: bool = False,
) -> list[CaseResult]:
    core = JarvisCore(_isolated_config(cfg, model), providers=providers, persist=False)
    # Safety: simulate every action; never wait long for a confirmation.
    await core.update_settings({"dry_run_computer": True, "dry_run_smart_home": True})
    core.confirmations._timeout_s = 1.0
    available = {t.name for t in core.registry.all()}
    results: list[CaseResult] = []
    try:
        for case in cases or CASES:
            if not case.expected <= available:
                continue  # e.g. computer tools on a non-Windows machine
            core.orchestrator.reset_conversation()
            core.history.clear()
            started = time.monotonic()
            res = await core.orchestrator.handle(case.prompt, speech=spoken)
            called = [e["tool"] for e in reversed(core.history.list())]
            passed = bool(res.get("ok")) and (
                case.expected <= set(called) if case.expected else not called
            )
            error = None if res.get("ok") else res.get("error")
            if spoken and passed and guess_language(clean_for_speech(res.get("text") or "")) != case.language:
                passed, error = False, f"reply not in {case.language}: {res.get('text')}"
            results.append(
                CaseResult(
                    case=case,
                    called=called,
                    passed=passed,
                    seconds=round(time.monotonic() - started, 1),
                    reply=(res.get("text") or "").replace("\n", " "),
                    error=error,
                )
            )
    finally:
        await core.stop()
    return results


def format_report(results: list[CaseResult], model: str, spoken: bool = False) -> str:
    mode = ", spoken requests" if spoken else ""
    lines = [f"Tool-use check — model: {model} (dry-run, local only{mode})", ""]
    for r in results:
        mark = "PASS" if r.passed else "FAIL"
        expected = ", ".join(sorted(r.case.expected)) or "(no tool)"
        called = ", ".join(r.called) or "(none)"
        lines.append(f"[{mark}] {r.seconds:>5}s  {r.case.prompt}")
        lines.append(f"         expected: {expected}   called: {called}")
        detail = r.error or r.reply
        if not r.passed and detail:
            lines.append(f"         reply: {detail[:140]}")
    passed = sum(r.passed for r in results)
    lines += ["", f"Result: {passed}/{len(results)} passed"]
    return "\n".join(lines)
