import pytest

from jarvis.computer import windows
from jarvis.computer.windows import ComputerError, WindowsControl
from jarvis.config import ComputerConfig


def test_only_allowlisted_apps(monkeypatch):
    wc = WindowsControl(ComputerConfig())
    with pytest.raises(ComputerError, match="not an approved"):
        wc.open_app("powershell", dry_run=True)
    with pytest.raises(ComputerError):
        wc.open_app("C:\\Windows\\System32\\cmd.exe", dry_run=True)
    assert wc.open_app("Notepad", dry_run=True) == {"app": "notepad", "would_run": ["notepad.exe"]}


def test_live_launch_uses_fixed_argv_without_shell(monkeypatch):
    calls = {}

    class FakeProc:
        pid = 4242

    def fake_popen(argv, **kw):
        calls["argv"], calls["kw"] = argv, kw
        return FakeProc()

    monkeypatch.setattr(windows, "IS_WINDOWS", True)
    monkeypatch.setattr(windows.subprocess, "Popen", fake_popen)
    out = WindowsControl(ComputerConfig()).open_app("calculator", dry_run=False)
    assert out == {"app": "calculator", "launched": True, "pid": 4242}
    assert calls["argv"] == ["calc.exe"] and calls["kw"]["shell"] is False


def test_non_windows_live_actions_refused(monkeypatch):
    monkeypatch.setattr(windows, "IS_WINDOWS", False)
    wc = WindowsControl(ComputerConfig())
    with pytest.raises(ComputerError, match="only supported on Windows"):
        wc.open_app("notepad", dry_run=False)
    with pytest.raises(ComputerError):
        wc.list_windows()


def test_focus_window_matching(monkeypatch):
    monkeypatch.setattr(windows, "IS_WINDOWS", True)
    wins = [windows.WindowInfo(1, "Untitled - Notepad", 1), windows.WindowInfo(2, "Inbox - Outlook", 2), windows.WindowInfo(3, "Notes - Notepad", 3)]
    monkeypatch.setattr(windows, "_enum_windows", lambda: wins)
    focused = []
    monkeypatch.setattr(windows, "_set_foreground", lambda h: focused.append(h) or True)
    wc = WindowsControl(ComputerConfig())
    with pytest.raises(ComputerError, match="2 windows match"):
        wc.focus_window("notepad", dry_run=False)
    assert wc.focus_window("outlook", dry_run=True) == {"would_focus": "Inbox - Outlook"}
    assert focused == []
    assert wc.focus_window("outlook", dry_run=False) == {"focused": "Inbox - Outlook", "verified": True}
    assert focused == [2]
