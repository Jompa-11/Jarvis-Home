"""Advanced mode: run a PowerShell command, with the owner's explicit approval.

This is the one place where JARVIS runs something it was not given as a typed
tool, so it is fenced in on every side:

* Off until the owner turns on "Avancerat läge" in Settings.
* The exact command is shown and must be approved for every single run
  (``Risk.HIGH``), as the owner's normal user. Never elevated.
* Refused for the rest of a turn in which JARVIS has read e-mail, files, the
  clipboard, window titles or a web page, so text from a message can never
  reach a command (the executor enforces this, see ``ToolSpec.blocked_after_untrusted``).
* A blocklist that always applies: firewall, network and Defender changes,
  execution policy, download-and-run, and anything touching .env or the
  Credential Manager.
* 60 second timeout, at most 4000 characters of output, and the command is logged.
"""

from __future__ import annotations

import locale
import logging
import re
import subprocess
from collections.abc import Callable
from typing import Any

log = logging.getLogger(__name__)

TIMEOUT_S = 60.0
MAX_OUTPUT_CHARS = 4000
MAX_COMMAND_CHARS = 1000
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

POWERSHELL = ["powershell.exe", "-NoProfile", "-NonInteractive", "-NoLogo", "-Command"]

# (pattern, what to tell the owner). Checked case-insensitively on the whole command.
BLOCKED: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\b(new|set|remove|enable|disable)-netfirewall\w*", re.I), "ändrar brandväggen"),
    (re.compile(r"\bnetsh\b", re.I), "ändrar nätverk eller brandvägg"),
    (re.compile(r"\b(set|new|remove)-net(ipaddress|adapter|connectionprofile|route)\w*", re.I), "ändrar nätverket"),
    (re.compile(r"\b(disable|enable)-netadapter\w*", re.I), "ändrar nätverkskortet"),
    (re.compile(r"\bset-dnsclient\w*", re.I), "ändrar DNS"),
    (re.compile(r"\b(set|add|remove)-mppreference\b|\bmpcmdrun\b|\bdefender\b", re.I), "ändrar Defender"),
    (re.compile(r"\bset-executionpolicy\b", re.I), "ändrar ExecutionPolicy"),
    (re.compile(r"\b(iex|invoke-expression)\b", re.I), "kör nedladdad kod (Invoke-Expression)"),
    (re.compile(r"\bdownloadstring\b|\bdownloadfile\b", re.I), "laddar ner och kör kod"),
    (re.compile(r"\b(iwr|irm|invoke-webrequest|invoke-restmethod|curl|wget|start-bitstransfer)\b[^|]*\|", re.I),
     "skickar en nedladdning vidare till ett annat kommando"),
    (re.compile(r"\.env\b", re.I), "rör .env"),
    (re.compile(r"\b(cmdkey|vaultcmd|get-credential|credentialmanager|get-storedcredential)\b", re.I),
     "rör Credential Manager eller lösenord"),
    (re.compile(r"-verb\s+runas|\bstart-process\b[^\n]*\brunas\b", re.I), "försöker köra som administratör"),
    (re.compile(r"\benter-pssession\b|\binvoke-command\b.*-computername", re.I), "kör på en annan dator"),
    (re.compile(r"\bconvertto-securestring\b|\bexport-clixml\b.*cred", re.I), "hanterar sparade inloggningar"),
]


class ShellError(Exception):
    pass


def check_command(command: str) -> str:
    """The command, or ShellError explaining what is blocked."""
    text = str(command).strip()
    if not text:
        raise ShellError("Inget kommando angavs.")
    if len(text) > MAX_COMMAND_CHARS:
        raise ShellError(f"Kommandot får vara högst {MAX_COMMAND_CHARS} tecken.")
    if "\x00" in text:
        raise ShellError("Kommandot innehåller ogiltiga tecken.")
    for pattern, why in BLOCKED:
        if pattern.search(text):
            raise ShellError(f"Blockerat: kommandot {why}. Det gör JARVIS aldrig.")
    return text


def _decode(data: bytes | str | None) -> str:
    """Windows PowerShell output, whatever code page it used. Never raises."""
    if data is None:
        return ""
    if isinstance(data, str):
        return data
    for encoding in ("utf-8", locale.getpreferredencoding(False), "cp850"):
        try:
            return data.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    return data.decode("utf-8", errors="replace")


class PowerShellRunner:
    def __init__(self, *, run: Callable[..., Any] | None = None) -> None:
        self._run = run or subprocess.run

    def run(self, command: str, *, timeout_s: float = TIMEOUT_S) -> dict[str, Any]:
        cmd = check_command(command)
        # Logged so every run can be traced. Secrets never reach here: the
        # blocklist refuses .env and the Credential Manager.
        log.info("Advanced mode: running PowerShell: %s", cmd)
        try:
            # Bytes, not text: Windows PowerShell writes in the ANSI code page, which
            # raises UnicodeDecodeError in text mode on some output.
            proc = self._run(
                POWERSHELL + [cmd], capture_output=True, timeout=timeout_s,
                creationflags=_NO_WINDOW, shell=False,  # noqa: S603 - one argv element, never a shell line
            )
        except subprocess.TimeoutExpired:
            raise ShellError(f"Kommandot tog mer än {timeout_s:.0f} sekunder och avbröts.") from None
        except OSError as exc:
            raise ShellError(f"Kunde inte köra PowerShell ({type(exc).__name__}).") from exc
        stdout, stderr = _decode(proc.stdout), _decode(proc.stderr)
        out = (stdout + (("\n" + stderr) if stderr else "")).strip()
        truncated = len(out) > MAX_OUTPUT_CHARS
        return {
            "command": cmd,
            "exit_code": proc.returncode,
            "output": out[:MAX_OUTPUT_CHARS],
            "truncated": truncated,
            "ok": proc.returncode == 0,
        }
