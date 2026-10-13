"""Step 1: the owner's rules, preferences in the prompt, and the rules_show tool."""

from __future__ import annotations

from datetime import datetime

import pytest
from conftest import ScriptedProvider

from jarvis.config import ConfigError
from jarvis.core.orchestrator import build_system_prompt
from jarvis.rules import RULES_EXAMPLE_PATH, NotificationRules, RulesConfig, build_preferences_section, load_rules


def test_example_rules_load_with_expected_defaults():
    r = load_rules(RULES_EXAMPLE_PATH)
    assert {"faktura", "säkerhetskod", "verifieringskod"} <= set(r.mail.important_keywords)
    assert r.mail.urgent_keywords == ["larm", "akut"]
    assert r.notifications.urgent_overrides_quiet_hours is True
    assert "skatteverket.se" in r.mail.important_domains
    n = r.notifications
    assert (n.quiet_hours_start, n.quiet_hours_end, n.morning_summary_time) == ("22:00", "07:00", "07:30")
    assert n.max_announcements_per_hour == 4
    assert r.preferences.notes  # example has free-text wishes


def test_missing_rules_file_falls_back_to_example(tmp_path):
    assert load_rules(tmp_path / "nope.toml") == load_rules(RULES_EXAMPLE_PATH)


def test_invalid_rules_are_reported_clearly(tmp_path):
    p = tmp_path / "rules.toml"
    p.write_text('[notifications]\nquiet_hours_start = "25:99"\n', encoding="utf-8")
    with pytest.raises(ConfigError, match="quiet_hours_start"):
        load_rules(p)
    p.write_text("[mail\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="invalid TOML"):
        load_rules(p)


def test_lists_are_cleaned():
    r = RulesConfig.model_validate({"mail": {"important_domains": ["@Bank.SE", "bank.se", " "], "important_keywords": ["Faktura", "faktura"]}})
    assert r.mail.important_domains == ["bank.se"]
    assert r.mail.important_keywords == ["Faktura"]


@pytest.mark.parametrize(
    "hhmm, quiet",
    [("21:59", False), ("22:00", True), ("23:30", True), ("03:00", True), ("06:59", True), ("07:00", False), ("12:00", False)],
)
def test_quiet_hours_span_midnight(hhmm, quiet):
    h, m = map(int, hhmm.split(":"))
    assert NotificationRules().is_quiet(datetime(2026, 10, 13, h, m)) is quiet


def test_quiet_hours_within_one_day_and_disabled():
    day = NotificationRules(quiet_hours_start="12:00", quiet_hours_end="13:00")
    assert day.is_quiet(datetime(2026, 1, 1, 12, 30)) and not day.is_quiet(datetime(2026, 1, 1, 13, 0))
    assert not NotificationRules(quiet_hours_start="00:00", quiet_hours_end="00:00").is_quiet(datetime(2026, 1, 1, 3, 0))


def test_preference_length_is_limited():
    with pytest.raises(ValueError):
        RulesConfig.model_validate({"preferences": {"notes": ["x" * 301]}})


# ------------------------------------------------------------------ prompt ----
def test_preferences_come_after_unchanged_rules_and_never_override_safety(make_core):
    rules = RulesConfig.model_validate({"preferences": {"notes": ["Tilltala mig som sir.", "Var kort."]}})
    core = make_core(rules=rules)
    prompt = core.orchestrator.system_prompt
    base = build_system_prompt(core.cfg)
    assert prompt.startswith(base)  # the safety and tool rules are untouched and first
    tail = prompt[len(base):]
    assert tail == build_preferences_section(rules)
    assert "Ägarens preferenser" in tail and "- Tilltala mig som sir." in tail and "- Var kort." in tail
    assert "NEVER override the safety and tool rules" in tail


def test_no_preferences_no_section():
    assert build_preferences_section(RulesConfig()) == ""
    assert build_preferences_section(None) == ""


# ----------------------------------------------------------------- the tool ----
async def test_rules_show_is_read_only_and_personal(make_core):
    rules = RulesConfig.model_validate({"mail": {"important_domains": ["bank.se"]}, "preferences": {"notes": ["Var kort."]}})
    model = ScriptedProvider("anthropic", "cloud", [[("rules_show", {})], "Här är dina regler."])
    core = make_core({"anthropic": model}, rules=rules, ai={"routes": {"conversation": [{"provider": "anthropic"}]}})
    spec = core.registry.get("rules_show")
    assert spec.risk.name == "READ" and spec.output_sensitivity == "personal" and spec.supports_dry_run is False
    r = await core.orchestrator.handle("Vilka regler har du?")
    assert r["ok"]
    h = core.history.list()[0]
    assert h["tool"] == "rules_show" and h["status"] == "succeeded" and h["sensitivity"] == "personal"
    assert h["result"] == "[personal data hidden]"  # not echoed into the shared history view
    data = (await spec.handler({}, None)).data
    assert data["mail"]["important_domains"] == ["bank.se"]
    assert data["quiet_hours"] == "22:00-07:00" and data["preferences"] == ["Var kort."]
    # Personal output is withheld from a cloud model (default cloud_max_sensitivity = normal).
    tool_msg = [m for m in model.requests[1]["messages"] if m.role == "tool"][0]
    assert "bank.se" not in tool_msg.content
