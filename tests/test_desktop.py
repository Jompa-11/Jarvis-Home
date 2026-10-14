"""Desktop app: one instance, the restart guard, primary monitor, X hides, game mode at start."""

from __future__ import annotations

import subprocess
import threading

import pytest

from jarvis.desktop import APP_ID
from jarvis.desktop.app import WindowState, show_at_start
from jarvis.desktop.instance import InstanceLock, ShowListener, request_show
from jarvis.desktop.supervisor import RestartPolicy, ServerSupervisor
from jarvis.desktop.winapi import Monitor, pick_primary, window_rect_on


# ------------------------------------------------------------ one instance ----
def test_only_one_instance_holds_the_lock(tmp_path):
    first, second = InstanceLock(tmp_path), InstanceLock(tmp_path)
    assert first.acquire() is True
    assert second.acquire() is False  # a second start must not run another server
    first.release()
    assert second.acquire() is True
    second.release()


def test_second_start_shows_the_running_window(tmp_path):
    shown = threading.Event()
    running = InstanceLock(tmp_path)
    assert running.acquire()
    listener = ShowListener(shown.set).start()
    running.write_info(listener.port, listener.secret)
    try:
        newcomer = InstanceLock(tmp_path)
        assert not newcomer.acquire()
        assert request_show(newcomer.read_info, attempts=3, delay_s=0.05) is True
        assert shown.wait(2)
    finally:
        listener.stop()
        running.release()
    assert not (tmp_path / "instance.json").exists()  # removed on exit


def test_show_needs_the_secret(tmp_path):
    shown = threading.Event()
    listener = ShowListener(shown.set).start()
    try:
        assert request_show(lambda: {"port": listener.port, "secret": "wrong"}, attempts=2, delay_s=0.01) is False
        assert request_show(lambda: None, attempts=2, delay_s=0.01) is False
        assert not shown.is_set()
    finally:
        listener.stop()


def test_listener_is_loopback_only():
    listener = ShowListener(lambda: None)
    try:
        assert listener._sock.getsockname()[0] == "127.0.0.1"
    finally:
        listener.stop()


# ---------------------------------------------------------- restart guard ----
class Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


def test_restart_policy_max_five_per_hour_with_backoff():
    clock = Clock()
    p = RestartPolicy(max_per_hour=5, backoff_s=(2, 5, 15, 30, 60), clock=clock)
    delays = []
    for _ in range(5):
        delays.append(p.on_crash())
        clock.t += 60
    assert delays == [2, 5, 15, 30, 60]
    assert p.on_crash() is None  # the 6th crash within the hour: give up
    clock.t = 3601  # only the first crash (at 0 s) is now more than an hour old
    assert p.on_crash() == 60


class FakeProc:
    """Exits on its own with ``code`` (a crash) unless stopped through stdin."""

    def __init__(self, code: int | None) -> None:
        self.code = code
        self.stopped = threading.Event()
        self.stdin = self
        self.terminated = False

    def close(self) -> None:  # stdin.close(): the desktop app asks the server to stop
        self.stopped.set()

    def wait(self, timeout: float | None = None) -> int:
        if self.code is not None:
            return self.code
        if not self.stopped.wait(timeout if timeout is not None else 5):
            raise subprocess.TimeoutExpired("server", timeout)
        return 0

    def poll(self) -> int | None:
        return self.code if self.code is not None else (0 if self.stopped.is_set() else None)

    def terminate(self) -> None:
        self.terminated = True
        self.stopped.set()

    def kill(self) -> None:
        self.terminate()


def test_crashes_are_restarted_then_it_gives_up_and_notifies():
    procs: list[FakeProc] = []
    gave_up = []

    def start():
        procs.append(FakeProc(code=1))
        return procs[-1]

    sup = ServerSupervisor(start, RestartPolicy(max_per_hour=5, backoff_s=(0,)), on_gave_up=lambda: gave_up.append(True))
    sup.run()
    assert len(procs) == 6  # first start + 5 restarts
    assert sup.gave_up and gave_up == [True]


def test_quit_is_not_a_crash():
    procs: list[FakeProc] = []
    sup = ServerSupervisor(lambda: procs.append(FakeProc(code=None)) or procs[-1], RestartPolicy(backoff_s=(0,)))
    sup.run_in_thread()
    for _ in range(100):
        if procs:
            break
        threading.Event().wait(0.01)
    sup.stop(timeout_s=2)
    assert len(procs) == 1 and not sup.gave_up and procs[0].stopped.is_set() and not procs[0].terminated


def test_a_crash_after_a_while_restarts_once():
    codes = iter([1, None])
    procs: list[FakeProc] = []

    def start():
        procs.append(FakeProc(code=next(codes)))
        return procs[-1]

    started = []
    sup = ServerSupervisor(start, RestartPolicy(backoff_s=(0,)), on_started=lambda: started.append(True))
    sup.run_in_thread()
    for _ in range(200):
        if len(procs) == 2:
            break
        threading.Event().wait(0.01)
    sup.stop(timeout_s=2)
    assert len(procs) == 2 and len(started) == 2 and not sup.gave_up


# --------------------------------------------------------- primary monitor ----
def mon(left, top, right, bottom, primary, taskbar=48):
    return Monitor(left, top, right, bottom, left, top, right, bottom - taskbar, primary)


def test_primary_monitor_is_picked_from_windows_flags():
    left_screen = mon(-2560, 0, 0, 1440, False)
    main = mon(0, 0, 1920, 1080, True)
    above = mon(0, -1080, 1920, 0, False)
    assert pick_primary([left_screen, above, main]) == main
    assert pick_primary([]) is None
    # Without a flag: the monitor at the origin.
    assert pick_primary([mon(-1920, 0, 0, 1080, False), mon(0, 0, 2560, 1440, False)]).left == 0


@pytest.mark.parametrize("m", [mon(0, 0, 1920, 1080, True), mon(-2560, 0, 0, 1440, True), mon(1920, -300, 3200, 724, True)])
def test_window_starts_inside_the_primary_work_area(m):
    x, y, w, h = window_rect_on(m)
    assert m.work_left <= x and x + w <= m.work_right
    assert m.work_top <= y and y + h <= m.work_bottom


# ------------------------------------------------------- window behaviour ----
def test_x_hides_instead_of_quitting():
    hidden = []
    state = WindowState(lambda: hidden.append(True))
    assert state.on_closing() is False  # pywebview: False cancels the close
    assert hidden == [True]
    state.quitting = True  # "Avsluta JARVIS"
    assert state.on_closing() is True and hidden == [True]


def test_game_running_keeps_the_window_in_the_background():
    assert show_at_start(None) is True
    assert show_at_start("cs2.exe") is False
    assert show_at_start("GPU 97 %") is False


def test_app_id_matches_the_shortcuts():
    from pathlib import Path

    script = (Path(__file__).resolve().parent.parent / "scripts" / "autostart.ps1").read_text(encoding="utf-8-sig")
    assert f"$AppId = '{APP_ID}'" in script
    assert "--background --startup" in script and "-m jarvis --background'" in script
    assert "HKLM" not in script and "New-Service" not in script and "RunAs" not in script.replace("RunAsAdministrator", "")


def test_serve_child_stops_when_stdin_closes():
    import os

    from jarvis.__main__ import _watch_stdin_for_stop

    class Server:
        should_exit = False

    r, w = os.pipe()
    saved = os.dup(0)
    try:
        os.dup2(r, 0)
        server = Server()
        _watch_stdin_for_stop(server)
        os.close(w)  # the desktop app closes the pipe
        for _ in range(100):
            if server.should_exit:
                break
            threading.Event().wait(0.02)
        assert server.should_exit
    finally:
        os.dup2(saved, 0)
        os.close(saved)
        os.close(r)
