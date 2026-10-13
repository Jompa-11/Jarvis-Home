"""Notification channels available now: the UI list and Windows notifications.

Voice and phone (Discord) channels implement the same ``NotificationChannel``
interface and are added later without changing the Notifier.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import os
import sys
from collections.abc import Callable
from typing import Any

from .notifier import Notification, NotificationChannel

log = logging.getLogger(__name__)

URGENT_PREFIX = "🚨 AKUT: "


def display_title(n: Notification) -> str:
    return (URGENT_PREFIX + n.title) if n.priority == "urgent" else n.title


class UIChannel(NotificationChannel):
    """The notification list in the JARVIS interface (passive)."""

    name = "ui"
    interrupting = False
    min_priority = "normal"

    def __init__(self, publish: Callable[[str, dict[str, Any]], None]) -> None:
        self._publish = publish

    async def send(self, n: Notification) -> bool:
        self._publish("notification", n.public())
        return True


# A fixed script: the text arrives in environment variables and is XML-escaped
# inside PowerShell, so notification text can never become code or markup.
_TOAST_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] > $null
[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] > $null
$t = [Security.SecurityElement]::Escape($env:JARVIS_TOAST_TITLE)
$b = [Security.SecurityElement]::Escape($env:JARVIS_TOAST_BODY)
$xml = New-Object Windows.Data.Xml.Dom.XmlDocument
$xml.LoadXml("<toast><visual><binding template='ToastGeneric'><text>$t</text><text>$b</text></binding></visual></toast>")
$toast = [Windows.UI.Notifications.ToastNotification]::new($xml)
$appId = '{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe'
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($appId).Show($toast)
"""
_ENCODED = base64.b64encode(_TOAST_SCRIPT.encode("utf-16-le")).decode("ascii")


class WindowsToastChannel(NotificationChannel):
    """A Windows notification (bottom right / Action Center). Interrupting."""

    name = "windows"
    interrupting = True
    min_priority = "high"

    def __init__(self, timeout_s: float = 10.0) -> None:
        if sys.platform != "win32":
            raise RuntimeError("Windows notifications are only available on Windows")
        self.timeout_s = timeout_s

    async def send(self, n: Notification) -> bool:
        env = dict(os.environ)
        env["JARVIS_TOAST_TITLE"] = "JARVIS · " + display_title(n)
        env["JARVIS_TOAST_BODY"] = n.detail or ""
        try:
            proc = await asyncio.create_subprocess_exec(
                "powershell.exe", "-NoProfile", "-NonInteractive", "-EncodedCommand", _ENCODED,
                env=env, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
                creationflags=getattr(__import__("subprocess"), "CREATE_NO_WINDOW", 0),
            )
            _, err = await asyncio.wait_for(proc.communicate(), timeout=self.timeout_s)
        except Exception as exc:
            log.warning("Windows notification failed: %s", type(exc).__name__)
            return False
        if proc.returncode != 0:
            log.warning("Windows notification failed (exit %s)", proc.returncode)
            return False
        return True
