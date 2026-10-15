"""Everyday commands, handled without the AI model.

A small local model is slow and unreliable for "öppna spotify" or "volym 30": it
mangles names and sometimes claims it did something it did not. These rules run
BEFORE the model, cost nothing and cannot hallucinate.

A rule only fires when it is certain. Anything else goes to the model as before.
The tool call still goes through the executor, so permissions, confirmations,
the untrusted-data rules and dry-run all apply exactly as they do for the model.
The reply is a fixed Swedish sentence built from the tool's own result, so
JARVIS never says more than the tool reported.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from .tools import ToolSpec

PROVIDER = "fast"  # shown as "SNABB" in the interface


@dataclass(frozen=True)
class FastCall:
    tool: str
    args: dict[str, Any]
    reply: Callable[[dict[str, Any], bool], str]  # (data, dry_run) -> Swedish sentence


# Words that mean a folder rather than a program.
FOLDER_WORDS = r"(hämtade filer|hämtade|nedladdningar|downloads|dokument|documents|skrivbordet|skrivbord|desktop|bilder|pictures)"
SITE_WORDS = r"(youtube|google|gmail|facebook|instagram|tiktok|netflix|reddit|wikipedia|twitch|svt|svt play|aftonbladet|expressen|blocket|github|chatgpt|x|twitter|linkedin)"
_DOMAIN = re.compile(r"^[\w-]+(\.[\w-]+)+(/\S*)?$", re.I)

MEDIA_WORDS = {
    "pausa": "pause", "paus": "pause", "pause": "pause", "stoppa musiken": "pause",
    "spela": "play", "spela upp": "play", "fortsätt": "play", "play": "play", "resume": "play",
    "nästa": "next", "nästa låt": "next", "next": "next", "next track": "next",
    "föregående": "previous", "föregående låt": "previous", "previous": "previous", "back": "previous",
}


def _clean(target: str) -> str:
    return target.strip().strip("\"'.!?,").strip()


# -- reply templates ---------------------------------------------------------
def _reply_open_app(data: dict, dry_run: bool) -> str:
    app = data.get("app", "Programmet")
    if dry_run:
        return f"Skulle ha startat {app} (dry-run, inget hände)."
    if data.get("already_running"):
        if data.get("focused"):
            return f"{app} var redan öppet, jag tog fram det."
        return f"{app} var redan öppet, men jag kunde inte ta fram fönstret."
    if data.get("verified"):
        return f"{app} är startat."
    return f"Jag startade {app}, men ser inget fönster än."


def _reply_window(data: dict, dry_run: bool) -> str:
    window = data.get("window") or data.get("would") or "fönstret"
    action = data.get("action") or data.get("would")
    if dry_run:
        return f"Skulle ha gjort {action} på {window} (dry-run)."
    verbs = {"close": "Stängde", "focus": "Växlade till", "minimize": "Minimerade", "maximize": "Maximerade", "restore": "Återställde"}
    verb = verbs.get(str(action), "Gjorde")
    if action == "focus" and data.get("verified") is False:
        return f"Jag försökte växla till {window}, men Windows lät mig inte."
    return f"{verb} {window}."


def _reply_volume(data: dict, dry_run: bool) -> str:
    if dry_run:
        return f"Skulle ha satt volymen till {data.get('would_set_volume')} procent (dry-run)."
    level = data.get("volume")
    if data.get("muted"):
        return f"Volymen är {level} procent, men ljudet är tystat."
    return f"Volymen är nu {level} procent."


def _reply_mute(data: dict, dry_run: bool) -> str:
    if dry_run:
        return "Skulle ha ändrat ljudet (dry-run)."
    return "Ljudet är tystat." if data.get("muted") else "Ljudet är på igen."


def _reply_media(data: dict, dry_run: bool) -> str:
    action = data.get("pressed") or data.get("would_press")
    words = {"pause": "Pausade musiken.", "play": "Spelar.", "next": "Nästa låt.", "previous": "Föregående låt.", "stop": "Stoppade."}
    text = words.get(str(action), "Klart.")
    return f"Skulle ha gjort det (dry-run): {text}" if dry_run else text


def _reply_web(data: dict, dry_run: bool) -> str:
    if dry_run:
        return f"Skulle ha öppnat {data.get('would_open')} (dry-run)."
    return f"Öppnade {data.get('opened')}."


def _reply_search(data: dict, dry_run: bool) -> str:
    what = data.get("searched") or data.get("would_search")
    return f"Skulle ha sökt på {what} (dry-run)." if dry_run else f"Sökte på {what} i webbläsaren."


def _reply_folder(data: dict, dry_run: bool) -> str:
    where = data.get("opened") or data.get("would_open")
    return f"Skulle ha öppnat {where} (dry-run)." if dry_run else f"Öppnade {where}."


def _reply_lock(data: dict, dry_run: bool) -> str:
    return "Skulle ha låst datorn (dry-run)." if dry_run else "Låser datorn."


def _reply_time(data: dict, dry_run: bool) -> str:
    stamp = str(data.get("local_time", ""))
    clock = stamp[11:16] if len(stamp) >= 16 else stamp  # "2026-10-15 11:47:50" -> "11:47"
    return f"Klockan är {clock}."


def _reply_summary(data: dict, dry_run: bool) -> str:
    return str(data.get("title") or "Här är sammanfattningen.")


# -- the rules ---------------------------------------------------------------
def _open_something(target: str) -> FastCall | None:
    target = _clean(target)
    if not target or len(target) > 60:
        return None
    if re.fullmatch(FOLDER_WORDS, target, re.I):
        return FastCall("computer_open_folder", {"folder": target}, _reply_folder)
    if re.fullmatch(SITE_WORDS, target, re.I) or _DOMAIN.match(target):
        return FastCall("computer_open_web", {"site": target}, _reply_web)
    return FastCall("computer_open_app", {"app": target}, _reply_open_app)


def match(text: str) -> FastCall | None:
    """The tool call this text clearly means, or None to let the model handle it."""
    t = _clean(text).lower()
    if not t or len(t) > 120:
        return None

    if m := re.fullmatch(r"(?:öppna|starta|start|open|launch|kör igång)\s+(.{1,60})", t):
        return _open_something(m.group(1))
    if m := re.fullmatch(r"(?:stäng|stäng ner|close)\s+(.{1,60})", t):
        return FastCall("computer_window", {"title_contains": _clean(m.group(1)), "action": "close"}, _reply_window)
    if m := re.fullmatch(r"(?:byt till|växla till|gå till|switch to|fokusera(?: på)?)\s+(.{1,60})", t):
        return FastCall("computer_window", {"title_contains": _clean(m.group(1)), "action": "focus"}, _reply_window)
    if m := re.fullmatch(r"(?:sök(?:a)?(?: på| efter)?|google|search(?: for)?)\s+(.{1,100})", t):
        return FastCall("computer_search_web", {"query": _clean(m.group(1))}, _reply_search)

    if m := re.fullmatch(r"(?:sätt |ställ |ändra )?volym(?:en)?(?: (?:på|till))?\s*(\d{1,3})(?:\s*(?:%|procent))?", t) or \
            re.fullmatch(r"(?:set )?volume(?: to)?\s*(\d{1,3})\s*%?", t):
        return FastCall("volume_set", {"level": min(100, int(m.group(1)))}, _reply_volume)
    if re.fullmatch(r"(?:höj|öka|upp med)(?: volymen| ljudet)?|turn it up|volume up", t):
        return FastCall("volume_adjust", {"step": 10}, _reply_volume)
    if re.fullmatch(r"(?:sänk|minska|ner med)(?: volymen| ljudet)?|turn it down|volume down", t):
        return FastCall("volume_adjust", {"step": -10}, _reply_volume)
    if re.fullmatch(r"(?:tysta(?: ljudet| datorn)?|mute|stäng av ljudet)", t):
        return FastCall("volume_mute", {"muted": True}, _reply_mute)
    if re.fullmatch(r"(?:ljud(?:et)? på|slå på ljudet|unmute)", t):
        return FastCall("volume_mute", {"muted": False}, _reply_mute)

    if (action := MEDIA_WORDS.get(re.sub(r"\s+(musiken|låten|the music)$", "", t).strip())) is not None:
        return FastCall("media_control", {"action": action}, _reply_media)

    if re.fullmatch(r"(?:lås(?: datorn| skärmen)?|lock(?: the)?(?: pc| computer| screen)?)", t):
        return FastCall("power_lock", {}, _reply_lock)
    if re.fullmatch(r"(?:vad är klockan\??|hur mycket är klockan\??|what(?:'s| is) the time\??|what time is it\??)", t):
        return FastCall("system_get_time", {}, _reply_time)
    if re.fullmatch(r"(?:sammanfatta(?: dagen| nu| läget)?|dagens sammanfattning|summarize(?: the)?(?: day)?)", t):
        return FastCall("monitor_summary_now", {}, _reply_summary)
    return None


def usable(call: FastCall, tools: list[ToolSpec]) -> bool:
    """Only use the shortcut for a tool this profile is actually allowed to use."""
    return any(t.name == call.tool for t in tools)
