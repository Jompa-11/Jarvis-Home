"""Measure whether a model uses JARVIS's tools instead of answering from memory.

    python -m jarvis toolcheck                      # the configured Ollama model
    python -m jarvis toolcheck --model llama3.1:8b  # compare another local model

Every prompt runs through the real orchestrator with dry-run forced on for
both the computer and the smart home, so nothing is opened or changed.
Only the local Ollama provider is used, so the check costs nothing.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta

from .app_core import JarvisCore
from .config import JarvisConfig, ModelRoute
from .providers.base import AIProvider


@dataclass(frozen=True)
class Case:
    prompt: str
    expected: frozenset[str]  # tools that must be called; empty = no tool expected
    # The time is in every request's context, so a reply with the correct time
    # (HH:MM, +-1 minute) passes even without a tool call.
    time_in_reply_ok: bool = False


CASES: list[Case] = [
    Case("Vilka program får du starta?", frozenset({"computer_list_apps"})),
    Case("Öppna anteckningar", frozenset({"computer_open_app"})),
    Case("Starta miniräknaren", frozenset({"computer_open_app"})),
    Case("Vad är klockan?", frozenset({"system_get_time"}), time_in_reply_ok=True),
    Case("Visa systemstatus", frozenset({"system_get_status"})),
    Case("Which speakers do you have?", frozenset({"sonos_list_speakers"})),
    Case("Pausa musiken i köket", frozenset({"sonos_playback"})),
    Case("Hitta min senaste faktura", frozenset({"files_search"})),
    Case("Sammanfatta dagen", frozenset({"monitor_summary_now"})),
    Case("Berätta ett kort skämt", frozenset()),
]


_HHMM = re.compile(r"(?<!\d)([01]?\d|2[0-3])[:.]([0-5]\d)(?!\d)")


def reply_has_time(reply: str, start: datetime, end: datetime) -> bool:
    """True if the reply names a clock time between start-1 min and end+1 min."""
    lo = (start - timedelta(minutes=1)).replace(second=0, microsecond=0)
    hi = end + timedelta(minutes=1)
    for h, m in _HHMM.findall(reply):
        for day in (start.date(), end.date()):
            t = datetime.combine(day, datetime.min.time()).replace(hour=int(h), minute=int(m))
            if lo <= t <= hi:
                return True
    return False


@dataclass
class CaseResult:
    case: Case
    called: list[str]
    passed: bool
    seconds: float
    reply: str
    error: str | None = None
    via_context: bool = False  # passed with the correct time from the context, without a tool


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
            wall_start = datetime.now()
            res = await core.orchestrator.handle(case.prompt)
            called = [e["tool"] for e in reversed(core.history.list())]
            passed = bool(res.get("ok")) and (
                case.expected <= set(called) if case.expected else not called
            )
            via_context = False
            if not passed and res.get("ok") and case.time_in_reply_ok and not called:
                passed = via_context = reply_has_time(res.get("text") or "", wall_start, datetime.now())
            results.append(
                CaseResult(
                    case=case,
                    called=called,
                    passed=passed,
                    seconds=round(time.monotonic() - started, 1),
                    reply=(res.get("text") or "").replace("\n", " "),
                    error=None if res.get("ok") else res.get("error"),
                    via_context=via_context,
                )
            )
    finally:
        await core.stop()
    return results


def format_report(results: list[CaseResult], model: str) -> str:
    lines = [f"Tool-use check — model: {model} (dry-run, local only)", ""]
    for r in results:
        mark = "PASS" if r.passed else "FAIL"
        expected = ", ".join(sorted(r.case.expected)) or "(no tool)"
        called = ", ".join(r.called) or "(none)"
        if r.via_context:
            called += " (correct time from context)"
        lines.append(f"[{mark}] {r.seconds:>5}s  {r.case.prompt}")
        lines.append(f"         expected: {expected}   called: {called}")
        detail = r.error or r.reply
        if not r.passed and detail:
            lines.append(f"         reply: {detail[:140]}")
    passed = sum(r.passed for r in results)
    lines += ["", f"Result: {passed}/{len(results)} passed"]
    return "\n".join(lines)
