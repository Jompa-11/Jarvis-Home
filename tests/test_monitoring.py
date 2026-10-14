"""Step 2: scheduler, notifier (low/normal/high/urgent), channels and monitor_status.

A fixed fake clock and fake channels: no real Windows notifications, no waiting.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, time, timedelta

import pytest
from conftest import ScriptedProvider
from fastapi.testclient import TestClient

from jarvis.config import MonitoringConfig
from jarvis.monitoring import channels as channels_mod
from jarvis.monitoring.channels import URGENT_PREFIX, UIChannel, WindowsToastChannel, display_title
from jarvis.monitoring.notifier import Notification, NotificationChannel, Notifier, SeenStore, decide
from jarvis.monitoring.scheduler import AsyncScheduler, next_daily
from jarvis.monitoring.service import MonitorService
from jarvis.rules import NotificationRules, RulesConfig
from jarvis.server.app import create_app


class Clock:
    def __init__(self, at: datetime) -> None:
        self.now = at

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kw) -> None:
        self.now += timedelta(**kw)


class FakeChannel(NotificationChannel):
    def __init__(self, name: str, interrupting: bool, min_priority: str = "high", fail: bool = False) -> None:
        self.name, self.interrupting, self.min_priority, self.fail = name, interrupting, min_priority, fail
        self.sent: list[Notification] = []

    async def send(self, n: Notification) -> bool:
        if self.fail:
            raise RuntimeError("channel down")
        self.sent.append(n)
        return True


DAY = datetime(2026, 10, 13, 12, 0)  # outside quiet hours (22-07)
NIGHT = datetime(2026, 10, 13, 23, 30)


def make_notifier(at=DAY, rules=None, tmp_path=None):
    clock = Clock(at)
    ui, popup = FakeChannel("ui", False, "normal"), FakeChannel("windows", True, "high")
    seen = SeenStore(tmp_path / "state.json" if tmp_path else None, 14)
    n = Notifier(lambda: rules or NotificationRules(), channels=[ui, popup], seen=seen, clock=clock)
    return n, ui, popup, clock


# ------------------------------------------------------------------ decide ----
@pytest.mark.parametrize(
    "priority, kw, expected",
    [
        ("low", {}, "summary"),
        ("normal", {}, "passive"),
        ("high", {}, "immediate"),
        ("urgent", {}, "immediate"),
        ("high", {"quiet": True}, "summary"),
        ("urgent", {"quiet": True}, "immediate"),
        ("urgent", {"quiet": True, "urgent_overrides_quiet": False}, "summary"),
        ("high", {"muted": True}, "passive"),
        ("high", {"rate_ok": False}, "passive"),
        ("urgent", {"paused": True}, "summary"),
        ("normal", {"paused": True}, "summary"),
    ],
)
def test_delivery_decision(priority, kw, expected):
    args = {"quiet": False, "muted": False, "paused": False, "rate_ok": True, "urgent_overrides_quiet": True, **kw}
    assert decide(priority, **args)[0] == expected


# ---------------------------------------------------------------- notifier ----
async def test_priorities_route_to_the_right_channels():
    n, ui, popup, _ = make_notifier()
    low = await n.notify(source="mail", title="Nyhetsbrev", priority="low")
    normal = await n.notify(source="mail", title="Mejl från Anna", priority="normal")
    high = await n.notify(source="mail", title="Viktigt mejl från Skatteverket", priority="high")
    assert (low.delivery, normal.delivery, high.delivery) == ("summary", "passive", "immediate")
    assert [x.title for x in ui.sent] == ["Mejl från Anna", "Viktigt mejl från Skatteverket"]
    assert [x.title for x in popup.sent] == ["Viktigt mejl från Skatteverket"]
    assert [x.title for x in n.summary] == ["Nyhetsbrev", "Mejl från Anna"]  # what was not announced waits
    assert high.channels == ["ui", "windows"]


async def test_quiet_hours_save_everything_but_urgent_breaks_through():
    n, ui, popup, _ = make_notifier(at=NIGHT)
    high = await n.notify(source="mail", title="Faktura", priority="high")
    urgent = await n.notify(source="mail", title="Säkerhetskod", priority="urgent")
    assert high.delivery == "summary" and high.reason == "quiet hours"
    assert urgent.delivery == "immediate" and [x.title for x in popup.sent] == ["Säkerhetskod"]
    n2, _, popup2, _ = make_notifier(at=NIGHT, rules=NotificationRules(urgent_overrides_quiet_hours=False))
    assert (await n2.notify(source="mail", title="Larm", priority="urgent")).delivery == "summary" and not popup2.sent


async def test_hourly_limit_for_interruptions_but_not_for_urgent():
    n, _, popup, clock = make_notifier(rules=NotificationRules(max_announcements_per_hour=2))
    results = [await n.notify(source="mail", title=f"Viktig {i}", priority="high") for i in range(3)]
    assert [r.delivery for r in results] == ["immediate", "immediate", "passive"]
    assert results[2].reason == "hourly limit reached" and results[2].in_summary
    assert (await n.notify(source="mail", title="Akut", priority="urgent")).delivery == "immediate"
    clock.advance(minutes=61)
    assert (await n.notify(source="mail", title="Viktig igen", priority="high")).delivery == "immediate"
    assert len(popup.sent) == 4


async def test_mute_silences_high_not_urgent_and_pause_holds_everything():
    n, _, popup, clock = make_notifier()
    n.mute(60)
    assert n.muted
    assert (await n.notify(source="x", title="Viktig", priority="high")).delivery == "passive"
    assert (await n.notify(source="x", title="Akut", priority="urgent")).delivery == "immediate"
    clock.advance(minutes=61)
    assert not n.muted
    n.paused = True
    assert (await n.notify(source="x", title="Akut 2", priority="urgent")).delivery == "summary"
    assert [p.title for p in popup.sent] == ["Akut"]


async def test_same_event_is_never_notified_twice_even_after_restart(tmp_path):
    n, _, popup, _ = make_notifier(tmp_path=tmp_path)
    assert await n.notify(source="mail", title="Faktura", priority="high", dedupe_key="gmail:abc123")
    assert await n.notify(source="mail", title="Faktura", priority="high", dedupe_key="gmail:abc123") is None
    n2, _, popup2, _ = make_notifier(tmp_path=tmp_path)  # a new process reading the same state
    assert await n2.notify(source="mail", title="Faktura", priority="high", dedupe_key="gmail:abc123") is None
    assert len(popup.sent) == 1 and not popup2.sent
    stored = (tmp_path / "state.json").read_text(encoding="utf-8")
    assert "abc123" not in stored and "Faktura" not in stored  # only a hash and a time are stored


def test_seen_store_retention(tmp_path):
    s = SeenStore(tmp_path / "s.json", retention_days=14)
    now = datetime(2026, 10, 13).timestamp()
    s.add("old", now - 15 * 86400)
    s.add("new", now - 1 * 86400)
    assert s.prune(now) == 1 and s.seen("new") and not s.seen("old")
    assert set(json.loads((tmp_path / "s.json").read_text())["seen"]) == {SeenStore._h("new")}


async def test_a_failing_channel_never_breaks_notifications():
    clock = Clock(DAY)
    broken, ok = FakeChannel("broken", True, fail=True), FakeChannel("ok", True)
    n = Notifier(lambda: NotificationRules(), channels=[broken, ok], clock=clock)
    r = await n.notify(source="x", title="Viktig", priority="high")
    assert r.channels == ["ok"]


async def test_acknowledge_and_status():
    n, _, _, _ = make_notifier()
    a = await n.notify(source="x", title="A", priority="high")
    await n.notify(source="x", title="B", priority="normal")
    assert n.status()["unacknowledged"] == 2 and n.status()["today"] == 2
    assert n.acknowledge(a.id) == 1 and n.status()["unacknowledged"] == 1
    assert n.acknowledge() == 1 and n.status()["unacknowledged"] == 0


async def test_unknown_priority_rejected():
    n, *_ = make_notifier()
    with pytest.raises(ValueError):
        await n.notify(source="x", title="?", priority="critical")


# ------------------------------------------------------- one-time codes ----
@pytest.mark.parametrize(
    "text, expected",
    [
        ("Din säkerhetskod är 483920", "Din säkerhetskod är ••••"),
        ("Kod: 483 920", "Kod: ••••"),
        ("Kod 48-39-20 gäller i 5 min", "Kod •••• gäller i 5 min"),
        ("PIN 1234.", "PIN ••••."),
        ("Verifiering 12345678", "Verifiering ••••"),
        ("Två koder: 1111 och 2222", "Två koder: •••• och ••••"),
        ("Inget här, bara 123 och 7", "Inget här, bara 123 och 7"),  # under 4 digits stays
        ("Kontonummer 1234567890", "Kontonummer 1234567890"),  # 10 digits: not a one-time code, not cut in half
        ("Ring 070-123 45 67", "Ring 070-123 45 67"),  # a phone number is one longer run
        ("Klockan 07:30", "Klockan 07:30"),
    ],
)
def test_one_time_codes_are_masked(text, expected):
    from jarvis.monitoring.notifier import mask_codes

    assert mask_codes(text) == expected


async def test_codes_never_reach_channels_ui_or_summary(tmp_path):
    n, ui, popup, _ = make_notifier()
    events = []
    ui_real = UIChannel(lambda t, d: events.append(d))
    n.channels.append(ui_real)
    r = await n.notify(source="mail", title="Säkerhetskod från BankID: 483 920", priority="urgent", detail="Koden 112233 gäller i 5 minuter")
    assert r.title == "Säkerhetskod från BankID: ••••" and r.detail == "Koden •••• gäller i 5 minuter"
    for sent in (*ui.sent, *popup.sent):
        assert "483" not in sent.title and "112233" not in (sent.detail or "")
    # Only the text fields: a random hex id could contain "483" by chance.
    texts = [f"{e['title']} {e['detail']}" for e in (*events, *n.list_recent())]
    assert all("483" not in t and "112233" not in t for t in texts)
    assert "112233" not in str(n.status())
    low = await n.notify(source="mail", title="Kod 9876", priority="low")
    assert n.summary[-1].title == "Kod ••••" and low.title == "Kod ••••"


async def test_codes_are_masked_in_windows_notification(monkeypatch):
    captured = {}

    class Proc:
        returncode = 0

        async def communicate(self):
            return b"", b""

    async def fake_exec(*args, env=None, **kw):
        captured["env"] = env
        return Proc()

    monkeypatch.setattr(channels_mod.sys, "platform", "win32")
    monkeypatch.setattr(channels_mod.asyncio, "create_subprocess_exec", fake_exec)
    n = Notifier(lambda: NotificationRules(), channels=[WindowsToastChannel()], clock=Clock(DAY))
    await n.notify(source="mail", title="Verifieringskod 654321", priority="high", detail="Ange 654 321")
    assert captured["env"]["JARVIS_TOAST_TITLE"] == "JARVIS · Verifieringskod ••••"
    assert captured["env"]["JARVIS_TOAST_BODY"] == "Ange ••••"


# ---------------------------------------------------------------- channels ----
def test_urgent_is_clearly_marked():
    assert display_title(Notification("mail", "Larm från Securitas", "urgent")) == URGENT_PREFIX + "Larm från Securitas"
    assert display_title(Notification("mail", "Faktura", "high")) == "Faktura"


async def test_ui_channel_publishes_the_notification():
    events = []
    ch = UIChannel(lambda t, d: events.append((t, d)))
    assert ch.accepts(Notification("x", "n", "normal", delivery="passive"))
    assert not ch.accepts(Notification("x", "l", "low", delivery="summary"))
    assert await ch.send(Notification("x", "Hej", "normal"))
    assert events[0][0] == "notification" and events[0][1]["title"] == "Hej"


async def test_windows_notification_text_never_becomes_code(monkeypatch):
    if not hasattr(channels_mod, "WindowsToastChannel"):
        pytest.skip()
    captured = {}

    class Proc:
        returncode = 0

        async def communicate(self):
            return b"", b""

    async def fake_exec(*args, env=None, **kw):
        captured["args"], captured["env"] = args, env
        return Proc()

    monkeypatch.setattr(channels_mod.sys, "platform", "win32")
    monkeypatch.setattr(channels_mod.asyncio, "create_subprocess_exec", fake_exec)
    evil = "Hej'); Remove-Item C:\\ -Recurse; $(calc) <script>"
    assert await WindowsToastChannel().send(Notification("mail", evil, "urgent", detail="& more"))
    joined = " ".join(captured["args"])
    assert "Remove-Item" not in joined and "calc" not in joined  # the text is not part of the command
    assert captured["args"][:4] == ("powershell.exe", "-NoProfile", "-NonInteractive", "-EncodedCommand")
    assert captured["env"]["JARVIS_TOAST_TITLE"] == "JARVIS · " + URGENT_PREFIX + evil
    assert captured["env"]["JARVIS_TOAST_BODY"] == "& more"
    assert "SecurityElement]::Escape($env:JARVIS_TOAST_TITLE)" in channels_mod._TOAST_SCRIPT


async def test_windows_notification_failure_returns_false(monkeypatch):
    monkeypatch.setattr(channels_mod.sys, "platform", "win32")

    async def boom(*a, **k):
        raise FileNotFoundError("powershell.exe")

    monkeypatch.setattr(channels_mod.asyncio, "create_subprocess_exec", boom)
    assert await WindowsToastChannel().send(Notification("x", "t", "high")) is False


# --------------------------------------------------------------- scheduler ----
def test_next_daily():
    assert next_daily(datetime(2026, 10, 13, 7, 0), time(7, 30)) == datetime(2026, 10, 13, 7, 30)
    assert next_daily(datetime(2026, 10, 13, 7, 30), time(7, 30)) == datetime(2026, 10, 14, 7, 30)
    assert next_daily(datetime(2026, 10, 13, 23, 59), time(7, 30)) == datetime(2026, 10, 14, 7, 30)


async def test_periodic_job_survives_errors_and_stops_cleanly():
    s = AsyncScheduler()
    s.start()
    calls = []

    async def job():
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("temporary")

    info = s.every("check", 0.02, job, run_now=True)
    await asyncio.sleep(0.09)
    assert len(calls) >= 3 and info.runs >= 3
    assert info.last_error is None  # recovered after the first failure
    task = info.task
    await s.stop()
    assert task.done() and not s.running


async def test_reminder_uses_scheduler_interface():
    got = []

    async def on_reminder(profile, text):
        got.append((profile, text))

    s = AsyncScheduler(on_reminder=on_reminder)
    rid = await s.schedule("owner", datetime.now() + timedelta(milliseconds=30), "Ring tandläkaren")
    other = await s.schedule("owner", datetime.now() + timedelta(seconds=30), "Avbryts")
    await s.cancel(other)
    await asyncio.sleep(0.1)
    assert got == [("owner", "Ring tandläkaren")] and rid not in s.jobs and other not in s.jobs
    await s.stop()


# ----------------------------------------------------------------- service ----
async def test_morning_summary_collects_what_waited():
    clock = Clock(NIGHT)
    ui = FakeChannel("ui", False, "normal")
    m = MonitorService(MonitoringConfig(), lambda: RulesConfig(), channels=[ui], clock=clock)
    await m.notifier.notify(source="mail", title="Faktura från Telia", priority="high")
    await m.notifier.notify(source="files", title="Ny fil: kvitto.pdf", priority="low")
    clock.now = datetime(2026, 10, 14, 7, 30)
    summary = await m.morning_summary()
    assert summary.title == "Morgonsammanfattning: 2 saker som väntat."
    assert "Faktura från Telia" in summary.detail and "kvitto.pdf" in summary.detail
    assert summary.priority == "high" and not summary.acknowledged  # shown until acknowledged
    assert m.notifier.summary == [] and len(m.last_summary["items"]) == 2
    assert m.snapshot()["summary"]["acknowledged"] is False
    m.notifier.acknowledge(summary.id)
    assert m.snapshot()["summary"]["acknowledged"] is True
    clock.now = datetime(2026, 10, 14, 7, 31)
    empty = await m.morning_summary()
    assert empty.title == "Morgonsammanfattning: inget nytt sedan sist."


def test_sources_are_reported_truthfully():
    m = MonitorService(MonitoringConfig(), lambda: RulesConfig(), channels=[])
    assert {k: v["state"] for k, v in m.sources.items()} == {"mail": "not_configured", "folders": "not_configured"}


async def test_monitor_status_tool_is_read_only_and_personal(make_core):
    model = ScriptedProvider("anthropic", "cloud", [[("monitor_status", {})], "Inget särskilt har hänt."])
    core = make_core({"anthropic": model}, ai={"routes": {"conversation": [{"provider": "anthropic"}]}})
    spec = core.registry.get("monitor_status")
    assert spec.risk.name == "READ" and spec.output_sensitivity == "personal"
    await core.monitor.notifier.notify(source="mail", title="Viktigt mejl från banken", priority="high")
    data = (await spec.handler({}, None)).data
    assert data["monitoring"] == "active" and data["today"][0]["title"] == "Viktigt mejl från banken"
    assert data["watching"]["mail"]["state"] == "not_configured"
    r = await core.orchestrator.handle("Vad har hänt idag?")
    assert r["ok"] and core.history.list()[0]["tool"] == "monitor_status"
    tool_msg = [m for m in model.requests[1]["messages"] if m.role == "tool"][0]
    assert "banken" not in tool_msg.content  # personal data is withheld from a cloud model


def test_model_cannot_send_notifications(make_core):
    names = {t.name for t in make_core().registry.all()}
    assert not any(w in n for n in names for w in ("notify", "send", "toast", "webhook", "discord"))


async def test_core_starts_and_stops_monitoring_cleanly(make_core):
    core = make_core()
    await core.start()
    jobs = {j["name"] for j in core.monitor.scheduler.status()}
    assert {"morning_summary", "prune_state"} <= jobs
    tasks = [j.task for j in core.monitor.scheduler.jobs.values()]
    await core.stop()
    assert all(t.done() for t in tasks)


# ------------------------------------------------------------------ server ----
TOKEN = "t" * 40
H = {"X-Jarvis-Token": TOKEN}


def test_monitoring_endpoints(make_core):
    core = make_core()
    with TestClient(create_app(core, token=TOKEN, extra_hosts={"testserver"})) as c:
        assert c.post("/api/monitoring/pause", json={"paused": True}).status_code == 401
        s = c.get("/api/state", headers=H).json()["monitoring"]
        assert s["paused"] is False and s["recent"] == []
        assert c.post("/api/monitoring/pause", json={"paused": True}, headers=H).json()["paused"] is True
        assert core.settings.current.monitoring_paused is True  # persisted like other settings
        assert c.post("/api/notifications/mute", json={"minutes": 60}, headers=H).json()["muted"] is True
        assert c.post("/api/notifications/mute", json={"minutes": 0}, headers=H).json()["muted"] is False
        assert c.post("/api/notifications/mute", json={"minutes": -5}, headers=H).status_code == 422
        assert c.post("/api/monitoring/pause", json={"paused": False}, headers=H).json()["paused"] is False
        assert c.post("/api/notifications/ack", json={}, headers=H).json() == {"acknowledged": 0}
