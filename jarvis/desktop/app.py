"""JARVIS as a desktop app: ``python -m jarvis --background`` (run with pythonw.exe, no console).

* One instance (``instance.py``): a second start only shows the running window.
* The server runs in a supervised child process (``supervisor.py``).
* Own window (pywebview, Edge WebView2), maximized on the PRIMARY monitor. Closing
  it with X only hides it; JARVIS keeps running.
* Icon by the clock (pystray): Öppna JARVIS, Pausa/återuppta bevakning, Avsluta JARVIS.
* At login (``--startup``) the window waits ~10 s; if a game is running, the window
  stays in the background (icon only) so it never steals focus from the game.
"""

from __future__ import annotations

import logging
import os
import secrets
import threading
import time
from collections.abc import Callable
from typing import Any

import httpx

from ..config import PROJECT_ROOT, JarvisConfig
from . import APP_ID
from .instance import InstanceLock, ShowListener, request_show
from .supervisor import RestartPolicy, ServerSupervisor, notify_stopped, spawn_server
from .winapi import allow_foreground, bring_to_front, enable_dpi_awareness, maximize_on, primary_monitor, set_app_id

log = logging.getLogger(__name__)

ICON_PATH = PROJECT_ROOT / "assets" / "jarvis.ico"
STARTUP_DELAY_S = 10.0
TITLE = "J.A.R.V.I.S."
LOADING_HTML = (
    "<html><body style='margin:0;background:#02060d;color:#5fe3ff;font:15px Segoe UI,sans-serif;"
    "display:flex;align-items:center;justify-content:center;height:100vh'>JARVIS startar…</body></html>"
)


class WindowState:
    """Window decisions without any GUI, so they can be tested."""

    def __init__(self, hide: Callable[[], None]) -> None:
        self.hide = hide
        self.quitting = False

    def on_closing(self) -> bool:
        """pywebview "closing": False cancels the close. X only hides; Avsluta really closes."""
        if self.quitting:
            return True
        self.hide()
        return False


def show_at_start(game_reason: str | None) -> bool:
    """Open the window in the foreground at start, unless a game is running."""
    return game_reason is None


class DesktopApp:
    def __init__(self, cfg: JarvisConfig, *, startup: bool = False) -> None:
        self.cfg = cfg
        self.startup = startup
        self.url = f"http://{cfg.server.host}:{cfg.server.port}/"
        self.token = secrets.token_urlsafe(32)
        self.window: Any = None
        self.tray: Any = None
        self.state = WindowState(self._hide)
        self.supervisor: ServerSupervisor | None = None
        self._ready = threading.Event()

    # -- entry ---------------------------------------------------------------
    def run(self) -> int:
        enable_dpi_awareness()
        set_app_id(APP_ID)  # same taskbar button as the pinned shortcut
        lock = InstanceLock(self.cfg.data_dir)
        if not lock.acquire():
            info = lock.read_info()
            if info:
                allow_foreground(info.get("pid", 0))
            shown = request_show(lock.read_info)
            log.info("JARVIS is already running; %s", "showed its window" if shown else "could not reach it")
            return 0
        listener = ShowListener(self.show).start()
        lock.write_info(listener.port, listener.secret)
        try:
            return self._run_locked()
        finally:
            listener.stop()
            lock.release()

    def _run_locked(self) -> int:
        import webview  # type: ignore[import-not-found]

        from ..monitoring.gaming import GamingDetector

        game = GamingDetector(self.cfg.gaming).check_now()
        visible = show_at_start(game)
        if not visible:
            log.info("A game is running (%s): JARVIS starts in the background (icon only)", game)

        self.supervisor = ServerSupervisor(lambda: spawn_server(self.token), RestartPolicy(), on_started=self._server_started, on_gave_up=notify_stopped)
        self.supervisor.run_in_thread()
        self._start_tray()

        self.window = webview.create_window(
            TITLE, html=LOADING_HTML, width=1480, height=920, min_size=(980, 640), background_color="#02060d", hidden=True,
        )
        self.window.events.closing += self.state.on_closing
        icon = str(ICON_PATH) if ICON_PATH.exists() else None
        webview.start(self._after_gui_start, args=(visible,), icon=icon, private_mode=False, storage_path=str(self.cfg.data_dir / "webview"))
        # The GUI loop ended: Avsluta was chosen (or Windows is shutting down).
        self._shutdown()
        return 0

    def _after_gui_start(self, visible: bool) -> None:
        if self.startup:
            time.sleep(STARTUP_DELAY_S)  # let the PC finish starting before the window appears
        self._wait_for_server(timeout_s=90)
        self.window.load_url(self.url)
        if visible:
            self.show()

    # -- server --------------------------------------------------------------
    def _server_started(self) -> None:
        # After a restart the page must be reloaded: the new server has a fresh session token.
        if self.window is not None and self.supervisor is not None and self.supervisor.starts > 1:
            threading.Thread(target=self._reload_when_ready, daemon=True).start()

    def _reload_when_ready(self) -> None:
        if self._wait_for_server(timeout_s=90):
            self.window.load_url(self.url)

    def _wait_for_server(self, timeout_s: float) -> bool:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            try:
                if httpx.get(self.url + "api/health", timeout=2).status_code == 200:
                    return True
            except httpx.HTTPError:
                pass
            time.sleep(0.5)
        log.warning("The JARVIS server did not answer within %.0f s", timeout_s)
        return False

    def _api(self, method: str, path: str, **kw) -> dict[str, Any] | None:
        try:
            r = httpx.request(method, self.url + path.lstrip("/"), headers={"X-Jarvis-Token": self.token}, timeout=5, **kw)
            return r.json() if r.status_code == 200 else None
        except (httpx.HTTPError, ValueError):
            return None

    # -- window ----------------------------------------------------------------
    def _hwnd(self) -> int:
        try:
            return int(self.window.native.Handle.ToInt64())
        except Exception:
            return 0

    def show(self) -> None:
        if self.window is None:
            return
        self.window.show()
        monitor = primary_monitor()
        if monitor is not None:
            maximize_on(self._hwnd(), monitor)
        else:
            self.window.maximize()
        bring_to_front(self._hwnd())

    def _hide(self) -> None:
        # Called from the window's own closing event: hide right after it, not inside it.
        if self.window is not None:
            threading.Thread(target=self.window.hide, daemon=True).start()

    # -- tray ------------------------------------------------------------------
    def _monitoring_paused(self) -> bool | None:
        data = self._api("GET", "api/monitoring")
        return None if data is None else bool(data.get("paused"))

    def _toggle_pause(self) -> None:
        paused = self._monitoring_paused()
        if paused is None:
            log.warning("Could not read the monitoring state (server not ready, or not the owner profile)")
            return
        self._api("POST", "api/monitoring/pause", json={"paused": not paused})
        if self.tray is not None:
            self.tray.update_menu()

    def _start_tray(self) -> None:
        import pystray  # type: ignore[import-not-found]
        from PIL import Image

        image = Image.open(ICON_PATH) if ICON_PATH.exists() else Image.new("RGBA", (64, 64), (95, 227, 255, 255))

        def pause_text(_item) -> str:
            return "Återuppta bevakning" if self._monitoring_paused() else "Pausa bevakning"

        menu = pystray.Menu(
            pystray.MenuItem("Öppna JARVIS", lambda: self.show(), default=True),
            pystray.MenuItem(pause_text, lambda: self._toggle_pause()),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Avsluta JARVIS", lambda: self.quit()),
        )
        self.tray = pystray.Icon("JARVIS", image, "JARVIS", menu)
        threading.Thread(target=self.tray.run, name="jarvis-tray", daemon=True).start()

    # -- quit ------------------------------------------------------------------
    def quit(self) -> None:
        """Avsluta JARVIS: stop the server, remove the icon, close the window. Not a crash."""
        self.state.quitting = True
        if self.supervisor is not None:
            self.supervisor.stop()
        if self.tray is not None:
            self.tray.stop()
        if self.window is not None:
            self.window.destroy()

    def _shutdown(self) -> None:
        self.state.quitting = True
        if self.supervisor is not None and not self.supervisor.stopping:
            self.supervisor.stop()
        if self.tray is not None:
            try:
                self.tray.stop()
            except Exception:
                pass


def run_desktop(cfg: JarvisConfig, *, startup: bool = False) -> int:
    os.chdir(PROJECT_ROOT)
    return DesktopApp(cfg, startup=startup).run()
