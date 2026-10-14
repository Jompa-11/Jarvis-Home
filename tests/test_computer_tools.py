"""The PC tools: programs, web, folders, windows, volume, media, typing, clipboard, files, power.

Every Windows API is faked, so no test opens a program, changes the volume,
types anything, touches the clipboard or shuts anything down.
"""

from __future__ import annotations

import pytest
from conftest import FAKE_APPS, FakeDesktopAPI, ScriptedProvider

from jarvis.computer.apps import App, AppLauncher
from jarvis.computer.desktop import Desktop, parse_shortcut
from jarvis.computer.userfiles import FileActions
from jarvis.computer.web import WebOpener, resolve_target, search_url
from jarvis.computer.windows import ComputerError
from jarvis.config import ComputerConfig

CLOUD = {"ai": {"routes": {name: [{"provider": "anthropic"}] for name in ("conversation", "computer", "smart_home")}}}


def launcher(apps=None, **cfg):
    started: list[list[str]] = []
    lau = AppLauncher(ComputerConfig(**cfg), list_apps=lambda: list(apps if apps is not None else FAKE_APPS), launch=started.append)
    return lau, started


def desktop():
    api = FakeDesktopAPI()
    return Desktop(api=api), api


# ------------------------------------------------------------------ programs ----
@pytest.mark.parametrize("query, expected", [("spotify", "Spotify"), ("SPOTIFY", "Spotify"), ("discord", "Discord"),
                                             ("anteckningar", "Anteckningar"), ("chrome", "Google Chrome"), ("kalkylatorn", "Kalkylatorn")])
def test_starts_any_installed_program(query, expected):
    lau, started = launcher()
    data = lau.open(query, dry_run=False)
    assert data["app"] == expected and data["started"] is True
    assert started[0][0] == "explorer.exe" and started[0][1].startswith("shell:AppsFolder\\")


def test_allowlist_entries_stay_as_aliases():
    lau, started = launcher()  # default config has notepad/calculator/explorer
    assert lau.open("notepad", dry_run=False)["app"] == "Notepad"
    assert started == [["notepad.exe"]]  # the alias keeps its own fixed command line


def test_unknown_program_and_ambiguous_program_ask_instead_of_guessing():
    lau, started = launcher()
    with pytest.raises(ComputerError, match="Hittade inget installerat program"):
        lau.open("finnsinte", dry_run=False)
    several = [App("Firefox", "a"), App("Mozilla Firefox ESR", "b"), App("Waterfox (Firefox fork)", "c")]
    lau2, _ = launcher(apps=several)
    assert lau2.open("firefox", dry_run=False)["app"] == "Firefox"  # exact match wins
    lau3, _ = launcher(apps=several[1:])  # now only partial matches: ask instead of guessing
    with pytest.raises(ComputerError, match="Flera program matchar"):
        lau3.open("firefox", dry_run=False)
    assert started == []


def test_open_program_dry_run_starts_nothing():
    lau, started = launcher()
    assert lau.open("spotify", dry_run=True)["would_run"][0] == "explorer.exe"
    assert started == []


# ----------------------------------------------------------------------- web ----
@pytest.mark.parametrize("target, url", [
    ("youtube", "https://www.youtube.com"), ("YouTube", "https://www.youtube.com"),
    ("svt.se", "https://svt.se"), ("https://example.com/a?b=1", "https://example.com/a?b=1"),
    ("http://127.0.0.1:8765", "http://127.0.0.1:8765"),
])
def test_web_targets(target, url):
    assert resolve_target(target) == url


@pytest.mark.parametrize("bad", ["file:///C:/secret.txt", "javascript:alert(1)", "ftp://example.com", "mailto:a@b.se", "inte en adress"])
def test_only_http_and_https(bad):
    with pytest.raises(ComputerError):
        resolve_target(bad)


def test_search_builds_an_encoded_url():
    opened: list[str] = []
    web = WebOpener(open_url=opened.append)
    assert web.search("väder Stockholm", dry_run=False)["url"] == search_url("väder Stockholm")
    assert "v%C3%A4der+Stockholm" in opened[0]
    web.open_site("youtube", dry_run=True)
    assert len(opened) == 1  # dry-run opened nothing


# ------------------------------------------------------------- volume / media ----
def test_volume_set_adjust_and_mute():
    d, api = desktop()
    assert d.set_volume(30, dry_run=False)["volume"] == 30
    assert d.set_volume(250, dry_run=False)["volume"] == 100  # clamped
    assert d.set_volume(-5, dry_run=False)["volume"] == 0
    api.volume = 40
    assert d.adjust_volume(10, dry_run=False)["volume"] == 50
    assert d.adjust_volume(-80, dry_run=False)["volume"] == 0
    assert d.set_mute(True, dry_run=False)["muted"] is True
    assert d.set_mute(None, dry_run=False)["muted"] is False  # toggles back


def test_volume_and_media_dry_run_changes_nothing():
    d, api = desktop()
    assert d.set_volume(90, dry_run=True) == {"would_set_volume": 90}
    assert d.media("pause", dry_run=True) == {"would_press": "pause"}
    assert api.actions == [] and api.volume == 40


@pytest.mark.parametrize("action, vk", [("pause", 0xB3), ("play", 0xB3), ("next", 0xB0), ("previous", 0xB1)])
def test_media_keys(action, vk):
    d, api = desktop()
    d.media(action, dry_run=False)
    assert api.actions == [("keys", [vk])]


# ----------------------------------------------------------- typing / clipboard ----
@pytest.mark.parametrize("combo, vks", [("ctrl+s", [0x11, 0x53]), ("alt+tab", [0x12, 0x09]), ("win+e", [0x5B, 0x45]),
                                        ("ctrl+shift+t", [0x11, 0x10, 0x54]), ("f5", [0x74]), ("enter", [0x0D])])
def test_shortcuts(combo, vks):
    assert parse_shortcut(combo) == vks


@pytest.mark.parametrize("combo", ["ctrl+alt+del", "CTRL + ALT + DELETE", "ctrl+alt+delete"])
def test_ctrl_alt_del_is_refused(combo):
    with pytest.raises(ComputerError, match="Ctrl\\+Alt\\+Del"):
        parse_shortcut(combo)


def test_typing_and_clipboard():
    d, api = desktop()
    assert d.type_text("hej", dry_run=False)["typed_characters"] == 3
    assert api.actions == [("type", "hej")]
    assert d.type_text("x", dry_run=True)["into_window"] == "Testfönster"
    with pytest.raises(ComputerError):
        d.type_text("x" * 2001, dry_run=False)
    api.clipboard = "kopierat"
    assert d.read_clipboard()["text"] == "kopierat"
    d.write_clipboard("nytt", dry_run=False)
    assert api.clipboard == "nytt"
    d.write_clipboard("ignoreras", dry_run=True)
    assert api.clipboard == "nytt"


# ------------------------------------------------------------------- power ----
def test_lock_and_power_actions():
    d, api = desktop()
    assert d.lock(dry_run=True) == {"would": "lock"} and api.actions == []
    d.lock(dry_run=False)
    assert api.actions == [("lock",)]
    d.power("restart", dry_run=False)
    assert api.actions[-1] == ("power", "restart")
    with pytest.raises(ComputerError):
        d.power("format", dry_run=False)


# ------------------------------------------------------------------- files ----
def actions(tmp_path):
    home = tmp_path / "home"
    (home / "Documents").mkdir(parents=True)
    (home / "Downloads").mkdir()
    binned: list = []
    return FileActions(home=home, notes_folder=home / "Documents", recycle=lambda p: (binned.append(p), p.unlink())), home, binned


def test_create_note_rename_move_and_undo(tmp_path):
    fa, home, binned = actions(tmp_path)
    note = fa.create_note("inköp", "mjölk och bröd", dry_run=False)
    path = home / "Documents" / "inköp.txt"
    assert note["name"] == "inköp.txt" and path.read_text(encoding="utf-8") == "mjölk och bröd"

    renamed = fa.rename(str(path), "handla", dry_run=False)
    assert renamed["to"] == "handla.txt" and (home / "Documents" / "handla.txt").exists() and not path.exists()

    moved = fa.move(str(home / "Documents" / "handla.txt"), "hämtade filer", dry_run=False)
    assert moved["to"] == str(home / "Downloads") and (home / "Downloads" / "handla.txt").exists()

    assert "handla.txt" in fa.undo_last(dry_run=False)["result"]  # move undone
    assert (home / "Documents" / "handla.txt").exists()
    fa.undo_last(dry_run=False)  # rename undone
    assert path.exists()
    fa.undo_last(dry_run=False)  # creation undone -> Recycle Bin
    assert not path.exists() and binned
    with pytest.raises(ComputerError, match="inget att ångra"):
        fa.undo_last(dry_run=False)


def test_delete_goes_to_the_recycle_bin(tmp_path):
    fa, home, binned = actions(tmp_path)
    f = home / "Downloads" / "gammal.pdf"
    f.write_bytes(b"x")
    data = fa.to_recycle_bin(str(f), dry_run=False)
    assert binned == [f] and "Papperskorgen" in data["note"] and not f.exists()


def test_dry_run_file_actions_change_nothing(tmp_path):
    fa, home, _ = actions(tmp_path)
    f = home / "Downloads" / "a.txt"
    f.write_text("x", encoding="utf-8")
    assert "would_create" in fa.create_note("ny", "text", dry_run=True)
    assert fa.rename(str(f), "b", dry_run=True)["to"] == "b.txt"
    assert fa.move(str(f), "dokument", dry_run=True)["to"] == str(home / "Documents")
    assert fa.to_recycle_bin(str(f), dry_run=True)["would_move_to_recycle_bin"] == str(f)
    assert f.exists() and not (home / "Documents" / "ny.txt").exists() and fa.undo_stack == []


@pytest.mark.parametrize("bad", ["C:\\Windows\\System32\\drivers\\etc\\hosts", "..\\..\\Windows\\notepad.exe", "C:\\"])
def test_paths_outside_the_user_folder_are_refused(tmp_path, bad):
    fa, _, _ = actions(tmp_path)
    with pytest.raises(ComputerError, match="utanför din användarmapp|Hittar inte|skyddad"):
        fa.rename(bad, "x", dry_run=False)


def test_secret_files_and_protected_folders_are_refused(tmp_path):
    fa, home, _ = actions(tmp_path)
    (home / ".env").write_text("x", encoding="utf-8")
    (home / "AppData").mkdir()
    (home / "AppData" / "a.txt").write_text("x", encoding="utf-8")
    with pytest.raises(ComputerError, match="hemligheter"):
        fa.to_recycle_bin(str(home / ".env"), dry_run=False)
    with pytest.raises(ComputerError, match="skyddad"):
        fa.to_recycle_bin(str(home / "AppData" / "a.txt"), dry_run=False)


@pytest.mark.parametrize("name", ["a/b", "a\\b", "a:b", "a*b", "a?b", '"x"', "..", " "])
def test_bad_file_names_are_refused(tmp_path, name):
    fa, _, _ = actions(tmp_path)
    with pytest.raises(ComputerError):
        fa.create_note(name, "x", dry_run=False)


# ----------------------------------------------------------- through the core ----
async def test_owner_actions_run_without_confirmation_but_delete_asks(make_core):
    model = ScriptedProvider("anthropic", "cloud", [[("volume_set", {"level": 20})], "Volymen är 20."])
    core = make_core({"anthropic": model}, **CLOUD, computer={"enabled": True, "dry_run": False})
    res = await core.orchestrator.handle("sätt volymen på 20")
    assert res["ok"] and core.history.list()[0]["status"] == "succeeded"
    assert core.confirmations.pending() == []
    assert core.desktop.api.volume == 20

    spec = core.registry.get("files_recycle")
    assert spec.risk.name == "HIGH"  # deleting always asks
    assert core.registry.get("power_action").risk.name == "HIGH"
    assert core.registry.get("shell_run_powershell").risk.name == "HIGH"


async def test_tools_are_grouped_so_the_model_sees_a_few_at_a_time(make_core):
    from jarvis.core.tool_select import select_tools

    core = make_core()
    tools = core.registry.all()
    for text, expected in [
        ("öppna spotify", "computer_open_app"), ("sätt volymen på 30", "volume_set"), ("pausa musiken", "media_control"),
        ("öppna youtube", "computer_open_web"), ("öppna hämtade filer", "computer_open_folder"), ("lås datorn", "power_lock"),
        ("skapa en anteckning", "files_create_note"), ("ångra", "files_undo"),
    ]:
        selected, groups = select_tools(text, tools)
        names = {t.name for t in selected}
        assert expected in names, (text, sorted(names))
        assert len(selected) <= 22, (text, len(selected))


@pytest.mark.skipif(not __import__("sys").platform.startswith("win"), reason="Windows handles")
def test_windows_handles_are_typed_not_32_bit_ints():
    """A HANDLE returned as ctypes' default c_int is truncated on 64-bit Windows and
    crashes the process when dereferenced. This caught a real segfault in clipboard read."""
    import ctypes
    from ctypes import wintypes

    from jarvis.computer.winapi_desktop import _clipboard_api, _window_api

    user32, kernel32 = _clipboard_api()
    assert user32.GetClipboardData.restype is wintypes.HANDLE
    assert user32.SetClipboardData.restype is wintypes.HANDLE
    assert kernel32.GlobalAlloc.restype is wintypes.HGLOBAL
    assert kernel32.GlobalLock.restype is wintypes.LPVOID
    assert _window_api().GetForegroundWindow.restype is wintypes.HWND
    assert ctypes.sizeof(wintypes.HANDLE) == ctypes.sizeof(ctypes.c_void_p)
