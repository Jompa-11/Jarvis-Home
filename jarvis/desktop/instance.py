"""One JARVIS at a time.

The running desktop app holds an OS file lock on ``data/jarvis.lock`` (released by
Windows even if the process dies) and listens on a random loopback port for
"show". A second start finds the lock taken, reads ``data/instance.json`` (port +
a random secret, readable only by this user's data folder), asks the running app
to show its window and exits. It never starts a second server.
"""

from __future__ import annotations

import hmac
import json
import logging
import os
import secrets
import socket
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

SHOW = b"show"


class InstanceLock:
    def __init__(self, data_dir: Path) -> None:
        self.path = data_dir / "jarvis.lock"
        self.info_path = data_dir / "instance.json"
        self._fh: Any = None

    def acquire(self) -> bool:
        """True if this process is now the one JARVIS; False if another one runs."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(self.path, "a+b")  # noqa: SIM115 - held open for the life of the app
        try:
            if sys.platform == "win32":
                import msvcrt

                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            fh.close()
            return False
        self._fh = fh
        return True

    def release(self) -> None:
        if self._fh is None:
            return
        try:
            if sys.platform == "win32":
                import msvcrt

                self._fh.seek(0)
                msvcrt.locking(self._fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        self._fh.close()
        self._fh = None
        try:
            self.info_path.unlink()
        except OSError:
            pass

    def write_info(self, port: int, secret: str) -> None:
        self.info_path.write_text(json.dumps({"pid": os.getpid(), "port": port, "secret": secret}), encoding="utf-8")

    def read_info(self) -> dict[str, Any] | None:
        try:
            return json.loads(self.info_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None


class ShowListener:
    """Loopback-only listener; a message equal to the secret + "show" calls ``on_show``."""

    def __init__(self, on_show: Callable[[], None]) -> None:
        self.on_show = on_show
        self.secret = secrets.token_urlsafe(24)
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(4)
        self.port = self._sock.getsockname()[1]
        self._stop = False
        self._thread = threading.Thread(target=self._serve, name="jarvis-show-listener", daemon=True)

    def start(self) -> "ShowListener":
        self._thread.start()
        return self

    def _serve(self) -> None:
        while not self._stop:
            try:
                conn, _ = self._sock.accept()
            except OSError:
                return
            with conn:
                conn.settimeout(2)
                try:
                    data = conn.recv(256)
                except OSError:
                    continue
                expected = self.secret.encode() + b" " + SHOW
                if hmac.compare_digest(data.strip(), expected):
                    conn.sendall(b"ok")
                    try:
                        self.on_show()
                    except Exception:
                        log.exception("Showing the window failed")
                else:
                    conn.sendall(b"no")

    def stop(self) -> None:
        self._stop = True
        try:
            self._sock.close()
        except OSError:
            pass


def request_show(read_info: Callable[[], dict[str, Any] | None], *, attempts: int = 20, delay_s: float = 0.5) -> bool:
    """Ask the running JARVIS to show its window. Retries while it is still starting."""
    for _ in range(attempts):
        info = read_info()
        if info and info.get("port") and info.get("secret"):
            try:
                with socket.create_connection(("127.0.0.1", int(info["port"])), timeout=2) as s:
                    s.sendall(str(info["secret"]).encode() + b" " + SHOW)
                    if s.recv(16) == b"ok":
                        return True
            except OSError:
                pass
        time.sleep(delay_s)
    return False
