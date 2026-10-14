"""Step 4: metadata-only file index, files_search, files_open_folder and new-file notifications."""

from __future__ import annotations

import builtins
import io
import os
import sys
import time

import pytest
from conftest import ScriptedProvider

from jarvis.config import FilesConfig, MonitoringConfig
from jarvis.files.index import FileIndex, Root, is_secret_name, scan
from jarvis.files.watcher import FileWatcher
from jarvis.monitoring.service import MonitorService
from jarvis.providers.dev_provider import match_intent
from jarvis.rules import RulesConfig

RULES = RulesConfig.model_validate({"files": {"important_keywords": ["faktura", "kvitto", "avtal"]}})


def touch(path, *, mtime: float | None = None, data: bytes = b"x"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


def make_tree(base):
    """A watched folder with ordinary files, secrets, skipped folders and nesting."""
    w = base / "watched"
    touch(w / "semester.jpg")
    touch(w / "rapport.docx")
    touch(w / "sub" / "deep" / "anteckning.txt")
    for secret in (".env", "server.key", "cert.pem", "id.pfx", "vault.kdbx", "work.ovpn", "id_rsa", "id_rsa.pub", "mina lösenord.txt", "Passwords.xlsx"):
        touch(w / secret)
    for folder in (".ssh", ".git", "node_modules", "AppData"):
        touch(w / folder / "inside.txt")
    touch(w / "download.pdf.crdownload")
    return w


def make_watcher(tmp_path, root, *, rules=RULES, **cfg):
    monitor = MonitorService(MonitoringConfig(), lambda: rules, channels=[])
    fw = FileWatcher(FilesConfig(**cfg), monitor, lambda: rules, None, roots=[Root(root, "Hämtade filer")])
    return fw, monitor


# ------------------------------------------------------------------ scanning ----
def test_scan_skips_secrets_skipped_folders_and_temp_files(tmp_path):
    w = make_tree(tmp_path)
    records, truncated = scan([Root(w, "W")], max_depth=8, max_files=1000)
    names = sorted(r.name for r in records)
    assert names == ["anteckning.txt", "rapport.docx", "semester.jpg"]
    assert not truncated
    deep = next(r for r in records if r.name == "anteckning.txt")
    assert deep.rel_dir == os.path.join("sub", "deep") and deep.ext == "txt" and deep.root == "W"


@pytest.mark.parametrize("name", [".env", ".env.local", "prod.env", "a.KEY", "b.pem", "c.pfx", "d.kdbx", "e.ovpn", "id_rsa", "id_ed25519.pub", "Lösenord.docx", "credentials.json"])
def test_secret_names(name):
    assert is_secret_name(name)


def test_max_depth_and_max_files(tmp_path):
    w = make_tree(tmp_path)
    shallow, _ = scan([Root(w, "W")], max_depth=0, max_files=1000)
    assert sorted(r.name for r in shallow) == ["rapport.docx", "semester.jpg"]
    for i in range(30):
        touch(w / "many" / f"f{i}.txt")
    limited, truncated = scan([Root(w, "W")], max_depth=8, max_files=10)
    assert len(limited) == 10 and truncated


@pytest.mark.skipif(sys.platform != "win32", reason="hidden attribute is Windows-specific")
def test_hidden_and_system_files_are_skipped(tmp_path):
    import ctypes

    w = tmp_path / "w"
    hidden = touch(w / "hemlig.txt")
    touch(w / "synlig.txt")
    ctypes.windll.kernel32.SetFileAttributesW(str(hidden), 0x2)
    records, _ = scan([Root(w, "W")], max_depth=8, max_files=100)
    assert [r.name for r in records] == ["synlig.txt"]


def _make_link(link, target) -> str:
    """A junction (Windows, no admin needed) or a symlink. Returns the kind made."""
    if sys.platform == "win32":
        import _winapi

        _winapi.CreateJunction(str(target), str(link))
        return "junction"
    os.symlink(target, link, target_is_directory=True)
    return "symlink"


def test_links_out_of_the_watched_folder_are_not_followed(tmp_path):
    outside = tmp_path / "outside"
    touch(outside / "utanfor_faktura.pdf")
    w = tmp_path / "w"
    touch(w / "inne.txt")
    _make_link(w / "genvag", outside)
    try:
        os.symlink(outside / "utanfor_faktura.pdf", w / "fil_lank.pdf")
    except OSError:
        pass  # file symlinks need Developer Mode on Windows; the junction still covers folders
    records, _ = scan([Root(w, "W")], max_depth=8, max_files=100)
    assert [r.name for r in records] == ["inne.txt"]
    assert all(str(outside) not in r.path for r in records)


def test_scan_never_reads_file_contents(tmp_path, monkeypatch):
    w = make_tree(tmp_path)

    def forbidden(*a, **k):
        raise AssertionError("file contents must never be opened")

    monkeypatch.setattr(builtins, "open", forbidden)
    monkeypatch.setattr(io, "open", forbidden)
    monkeypatch.setattr(os, "open", forbidden)
    records, _ = scan([Root(w, "W")], max_depth=8, max_files=100)
    index = FileIndex(None)
    index.replace_all(records)
    assert index.count() == 3


# ------------------------------------------------------------------- search ----
def test_search_newest_first_max_20_type_and_period(tmp_path):
    w = tmp_path / "w"
    now = time.time()
    for i in range(25):
        touch(w / f"faktura_{i:02}.pdf", mtime=now - i * 3600)
    touch(w / "faktura_gammal.pdf", mtime=now - 40 * 86400)
    touch(w / "faktura_bild.png", mtime=now)
    touch(w / "annat.pdf", mtime=now)
    fw, _ = make_watcher(tmp_path, w)
    fw.index.replace_all(scan(fw.roots, max_depth=8, max_files=1000)[0])

    r = fw.search("faktura", "pdf", "any", now=now)
    names = [m["name"] for m in r["matches"]]
    assert len(names) == 20 and names[0] == "faktura_00.pdf" and names[1] == "faktura_01.pdf"
    assert "bevakade mapparna" in r["scope"] and "inte på hela datorn" in r["scope"]
    assert r["matches"][0]["folder"] == "Hämtade filer" and r["matches"][0]["path"].endswith("faktura_00.pdf")

    week = [m["name"] for m in fw.search("faktura", None, "week", now=now)["matches"]]
    assert "faktura_gammal.pdf" not in week and "faktura_bild.png" in week
    assert [m["name"] for m in fw.search("FAKTURA", "image", "any", now=now)["matches"]] == ["faktura_bild.png"]
    none = fw.search("finnsinte", None, "any", now=now)
    assert none["matches"] == [] and "No matching" in none["note"]


def test_like_wildcards_in_query_are_literal(tmp_path):
    w = tmp_path / "w"
    touch(w / "a_b.txt")
    touch(w / "axb.txt")
    fw, _ = make_watcher(tmp_path, w)
    fw.index.replace_all(scan(fw.roots, max_depth=8, max_files=100)[0])
    assert [m["name"] for m in fw.search("a_b")["matches"]] == ["a_b.txt"]
    assert fw.search("%")["matches"] == []


# ------------------------------------------------------------ notifications ----
async def test_first_scan_is_silent_then_new_files_notify_once(tmp_path):
    w = tmp_path / "w"
    touch(w / "gammal_faktura.pdf")
    fw, monitor = make_watcher(tmp_path, w)
    assert await fw.refresh() == 0  # baseline: existing files never notify
    assert monitor.notifier.list_recent() == []
    assert monitor.sources["folders"]["state"] == "active"

    touch(w / "testfaktura.pdf")
    touch(w / "semester.jpg")
    assert await fw.refresh() == 2
    recent = {n["title"]: n for n in monitor.notifier.list_recent()}
    hit = recent["Ny viktig fil: testfaktura.pdf"]
    assert hit["priority"] == "high" and "faktura" in hit["detail"] and "Hämtade filer" in hit["detail"]
    assert recent["Ny fil: semester.jpg"]["priority"] == "low"
    assert recent["Ny fil: semester.jpg"]["delivery"] == "summary"

    # Same file again (also after a delete and re-scan): never a second notification.
    assert await fw.refresh() == 0
    (w / "testfaktura.pdf").unlink()
    await fw.refresh()
    touch(w / "testfaktura.pdf")
    assert await fw.refresh() == 0
    assert len(monitor.notifier.list_recent()) == 2


async def test_many_new_files_are_grouped(tmp_path):
    w = tmp_path / "w"
    w.mkdir()
    fw, monitor = make_watcher(tmp_path, w)
    await fw.refresh()
    for i in range(30):
        touch(w / f"bild{i}.jpg")
    await fw.refresh()
    titles = [n["title"] for n in monitor.notifier.list_recent()]
    assert titles == ["30 nya filer"]
    assert len(fw.new_since_summary) == 30


async def test_secret_files_never_notify(tmp_path):
    w = tmp_path / "w"
    w.mkdir()
    fw, monitor = make_watcher(tmp_path, w)
    await fw.refresh()
    touch(w / "faktura.key")
    touch(w / ".env")
    assert await fw.refresh() == 0 and monitor.notifier.list_recent() == []


# ---------------------------------------------------------------- open folder ----
def test_open_folder_only_for_indexed_files_and_never_the_file(tmp_path, monkeypatch):
    import jarvis.files.watcher as watcher_mod

    w = tmp_path / "w"
    f = touch(w / "sub" / "testfaktura.pdf")
    other = touch(tmp_path / "elsewhere.pdf")
    fw, _ = make_watcher(tmp_path, w)
    fw.index.replace_all(scan(fw.roots, max_depth=8, max_files=100)[0])

    dry = fw.open_folder(str(f), dry_run=True)
    assert dry["would_run"].endswith(f'explorer.exe" /select,"{f}"') and dry["folder"] == "Hämtade filer\\sub"
    assert fw.open_folder("testfaktura.pdf", dry_run=True)["would_run"] == dry["would_run"]  # by name, from the index
    with pytest.raises(ValueError, match="not in the index"):
        fw.open_folder(str(other), dry_run=True)
    with pytest.raises(ValueError, match="not in the index"):
        fw.open_folder(str(w / "sub"), dry_run=True)

    launched = []
    monkeypatch.setattr(watcher_mod.subprocess, "Popen", lambda cmd, **k: launched.append(cmd))
    monkeypatch.setattr(watcher_mod.sys, "platform", "win32")
    assert fw.open_folder(str(f), dry_run=False)["opened"] is True
    assert launched == [dry["would_run"]]  # one ready string; Explorer selects the file, it is not opened


@pytest.mark.parametrize("name", ["faktura, kopia 2.pdf", "a.pdf,evil.exe", "a.pdf, ,select,x.exe .pdf", "två  mellanslag , och,komma.pdf"])
def test_open_folder_quotes_commas_and_spaces(tmp_path, name):
    w = tmp_path / "w"
    f = touch(w / name)
    fw, _ = make_watcher(tmp_path, w)
    fw.index.replace_all(scan(fw.roots, max_depth=8, max_files=100)[0])
    cmd = fw.open_folder(str(f), dry_run=True)["would_run"]
    # The whole path sits inside ONE pair of quotes right after "/select,".
    assert cmd.endswith(f'/select,"{f}"') and cmd.count('"') == 4
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        argc = ctypes.c_int()
        fn = ctypes.windll.shell32.CommandLineToArgvW
        fn.restype = ctypes.POINTER(wintypes.LPWSTR)
        argv = fn(cmd, ctypes.byref(argc))
        args = [argv[i] for i in range(argc.value)]
        ctypes.windll.kernel32.LocalFree(argv)
        assert args[1:] == [f"/select,{f}"]  # exactly one argument, commas and spaces included


@pytest.mark.parametrize("bad", ['C:\\x\\a".pdf', "C:\\x\\a\n.pdf", "relativ\\fil.pdf"])
def test_explorer_command_refuses_unsafe_paths(bad):
    from jarvis.files.watcher import explorer_select_command

    with pytest.raises(ValueError):
        explorer_select_command(bad)


# ------------------------------------------------------------- tools / core ----
async def test_tools_registered_with_declared_risk(make_core):
    core = make_core()
    s = core.registry.get("files_search")
    assert s.risk.name == "READ" and s.output_sensitivity == "personal" and not s.supports_dry_run
    o = core.registry.get("files_open_folder")
    assert o.risk.name == "MEDIUM" and o.supports_dry_run and o.category == "computer" and o.confirm_summary is not None


async def test_files_search_via_model_is_hidden_from_cloud(make_core, tmp_path):
    touch(tmp_path / "watched" / "testfaktura.pdf")
    model = ScriptedProvider("anthropic", "cloud", [[("files_search", {"query": "faktura"})], "Här."])
    core = make_core({"anthropic": model}, ai={"routes": {"conversation": [{"provider": "anthropic"}]}})
    await core.files.refresh()
    r = await core.orchestrator.handle("hitta min senaste faktura")
    assert r["ok"]
    h = core.history.list()[0]
    assert h["tool"] == "files_search" and h["result"] == "[personal data hidden]"
    tool_msg = [m for m in model.requests[1]["messages"] if m.role == "tool"][0]
    assert "testfaktura" not in tool_msg.content  # personal output never reaches a cloud model by default


async def test_dev_fallback_finds_invoice_and_opens_folder_dry_run(make_core, tmp_path):
    touch(tmp_path / "watched" / "testfaktura.pdf")
    core = make_core(ai={"routes": {"conversation": [{"provider": "dev"}]}})
    await core.files.refresh()
    r = await core.orchestrator.handle("hitta min senaste faktura")
    assert "testfaktura.pdf" in r["text"] and "bevakade mapparna" in r["text"]
    r2 = await core.orchestrator.handle("öppna mappen där den ligger")
    h = core.history.list()[0]
    assert h["tool"] == "files_open_folder" and h["dry_run"] is True
    assert "would open the folder" in r2["text"]


@pytest.mark.parametrize(
    "text, args",
    [
        ("hitta min senaste faktura", {"query": "faktura"}),
        ("Hitta fakturan från senaste veckan", {"query": "faktura", "period": "week"}),
        ("find my latest invoice", {"query": "invoice"}),
        ("sök efter filen testfaktura.pdf", {"query": "testfaktura.pdf"}),
        ('Sök efter filen "semester plan"', {"query": "semester plan"}),
        ("search for the file budget", {"query": "budget"}),
        ("leta efter kvittot som pdf", {"query": "kvitto", "file_type": "pdf"}),
    ],
)
def test_dev_file_intents(text, args):
    call = match_intent(text, {"files_search", "computer_open_app", "system_get_time"})
    assert call is not None and call.name == "files_search" and call.arguments == args


def test_unrelated_text_is_not_a_file_search():
    assert match_intent("öppna notepad", {"files_search", "computer_open_app"}).name == "computer_open_app"
    assert match_intent("vad är klockan?", {"files_search", "system_get_time"}).name == "system_get_time"


def test_file_rules_match_and_example():
    from jarvis.rules import RULES_EXAMPLE_PATH, load_rules

    assert {"faktura", "kvitto", "avtal"} <= set(load_rules(RULES_EXAMPLE_PATH).files.important_keywords)
    assert RULES.files.match("TestFaktura_2026.PDF") == "faktura"
    assert RULES.files.match("semester.jpg") is None


def test_known_folders_are_the_default():
    from jarvis.files.index import known_folder

    assert FilesConfig().watched_folders == ["known:Downloads", "known:Documents"]
    if sys.platform == "win32":
        assert known_folder("Downloads") is not None and known_folder("Downloads").is_dir()
