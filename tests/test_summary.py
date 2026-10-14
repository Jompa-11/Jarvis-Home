"""Step 5: the summary (important mail, new files, what waited), "Sammanfatta nu" and monitor_summary_now."""

from __future__ import annotations

import os
import time

from conftest import ScriptedProvider
from fake_mail import FakeMailProvider, mail
from fastapi.testclient import TestClient

from jarvis.providers.dev_provider import DevProvider, match_intent
from jarvis.rules import RulesConfig
from jarvis.server.app import create_app

RULES = RulesConfig.model_validate(
    {"mail": {"important_domains": ["skatteverket.se"]}, "files": {"important_keywords": ["faktura", "kvitto"]}}
)
PROVIDERS = {
    "anthropic": ScriptedProvider("anthropic", "cloud", [], available=False),
    "ollama": ScriptedProvider("ollama", "local", [], available=False),
    "dev": DevProvider(),
}


def touch(path, mtime=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x")
    if mtime is not None:
        os.utime(path, (mtime, mtime))


async def prepared_core(make_core, tmp_path):
    w = tmp_path / "watched"
    w.mkdir()
    provider = FakeMailProvider([mail("m1", "Skatteverket <info@skatteverket.se>", "Deklarationen är klar")])
    core = make_core(dict(PROVIDERS), rules=RULES, mail_provider=provider)
    await core.mail.poll()
    await core.files.refresh()  # baseline
    now = time.time()
    for i in range(6):
        touch(w / f"bild{i}.jpg", now - i * 60)
    touch(w / "testfaktura.pdf", now - 3600)  # oldest, but it matches a rule
    await core.files.refresh()
    await core.monitor.notifier.notify(source="reminder", title="Påminnelse: ring tandläkaren", priority="normal")
    return core


async def test_summary_has_mail_files_and_what_waited(make_core, tmp_path):
    core = await prepared_core(make_core, tmp_path)
    s = await core.monitor.summary_now()
    assert s["title"] == "Sammanfattning: 1 viktigt mejl, 7 nya filer, 1 sak som väntat."
    assert s["mail"] == ["Skatteverket (skatteverket.se): Deklarationen är klar."]
    assert s["files"]["count"] == 7 and len(s["files"]["top"]) == 5
    assert s["files"]["top"][0] == "testfaktura.pdf"  # the most important first
    assert s["files"]["top"][1:] == ["bild0.jpg", "bild1.jpg", "bild2.jpg", "bild3.jpg"]  # then newest
    assert [w["title"] for w in s["waited"]] == ["Påminnelse: ring tandläkaren"]  # files and important mail not repeated
    assert s["acknowledged"] is False

    # "sedan sist": a second summary right after has nothing new.
    again = await core.monitor.summary_now()
    assert again["title"] == "Sammanfattning: inget nytt sedan sist."


async def test_summary_shown_until_acknowledged(make_core, tmp_path):
    core = await prepared_core(make_core, tmp_path)
    s = await core.monitor.summary_now()
    snap = core.monitor.snapshot()
    assert snap["summary"]["notification_id"] == s["notification_id"] and not snap["summary"]["acknowledged"]
    n = next(x for x in snap["recent"] if x["id"] == s["notification_id"])
    assert n["kind"] == "summary" and n["delivery"] == "passive" and "testfaktura.pdf" in n["detail"]
    core.monitor.notifier.acknowledge(s["notification_id"])
    assert core.monitor.snapshot()["summary"]["acknowledged"] is True


async def test_scheduled_summary_pops_up_outside_quiet_hours(make_core, tmp_path):
    core = await prepared_core(make_core, tmp_path)
    n = await core.monitor.morning_summary()
    assert n.title.startswith("Morgonsammanfattning: ") and n.priority == ("normal" if RULES.notifications.is_quiet(core.monitor.clock()) else "high")


async def test_monitor_summary_now_tool(make_core, tmp_path):
    core = await prepared_core(make_core, tmp_path)
    spec = core.registry.get("monitor_summary_now")
    assert spec.risk.name == "LOW" and spec.output_sensitivity == "personal"
    r = await core.orchestrator.handle("sammanfatta dagen")  # dev fallback
    h = core.history.list()[0]
    assert h["tool"] == "monitor_summary_now" and h["status"] == "succeeded" and h["result"] == "[personal data hidden]"
    assert "1 viktigt mejl" in r["text"] and "testfaktura.pdf" in r["text"]


def test_dev_summary_intent():
    assert match_intent("Sammanfatta dagen", {"monitor_summary_now", "files_search"}).name == "monitor_summary_now"
    assert match_intent("summarize the day", {"monitor_summary_now"}).name == "monitor_summary_now"


async def test_summarize_now_button_endpoint(make_core, tmp_path):
    core = await prepared_core(make_core, tmp_path)
    app = create_app(core, token="t" * 32, extra_hosts={"testserver"})
    with TestClient(app) as c:
        assert c.post("/api/monitoring/summary").status_code == 401
        r = c.post("/api/monitoring/summary", headers={"X-Jarvis-Token": "t" * 32})
        assert r.status_code == 200
        assert r.json()["summary"]["title"].startswith("Sammanfattning: 1 viktigt mejl")
