"""Offer the model only the tool groups a request is about.

Small local models choose tools worse the more tools they see, so each turn
offers only the groups that free keyword rules recognise in the request (plus
the always-on system group). If nothing is recognised, every tool is offered.

This only narrows what the model is SHOWN. It is not a security boundary: the
executor still validates every call against the full registry, the role's
permissions, confirmation and dry-run.
"""

from __future__ import annotations

import re

from .tools import ToolSpec

ALWAYS = "system"  # time, status and rules_show are always offered

_GROUP_PATTERNS: dict[str, re.Pattern[str]] = {
    "smart_home": re.compile(
        r"\b(sonos|speakers?|högtalar\w*|music|musik\w*|volume|volym\w*|play\w*|spela\w*|paus\w*|resume|"
        r"favou?rit\w*|announc\w*|meddela\w*|lights?|lamp\w*|ljus\w*|home assistant|kök\w*|vardagsrum\w*|låt\w*|songs?)\b",
        re.I,
    ),
    "computer": re.compile(r"\b(open|launch|start\w*|öppna\w*|windows?|fönst\w*|app\w*|program\w*|anteckningar|miniräknar\w*|notepad|calculator)\b", re.I),
    "files": re.compile(
        r"\b(fil|filen|filer|filerna|files?|mapp\w*|folders?|dokument\w*|documents?|downloads?|hämtade|nedladdad\w*|"
        r"faktur\w*|invoices?|kvitt\w*|receipts?|avtal\w*|kontrakt\w*|contracts?|pdf|docx?|xlsx?)\b",
        re.I,
    ),
    "mail": re.compile(r"\b(mejl\w*|mail\w*|e-post\w*|epost\w*|inkorg\w*|inbox|gmail)\b", re.I),
    "monitor": re.compile(
        r"\b(hänt|happened|bevak\w*|watch\w*|notis\w*|notifications?|sammanfatt\w*|summar\w*|morgon\w*|briefing|"
        r"viktig\w*|important|idag|today|natten|overnight)\b",
        re.I,
    ),
    "system": re.compile(r"\b(klock\w*|tid|time|datum|date|status|system\w*|regl\w*|rules?)\b", re.I),
}

_PREFIX_GROUP = {
    "sonos": "smart_home",
    "computer": "computer",
    "files": "files",
    "mail": "mail",
    "monitor": "monitor",
    "system": "system",
    "rules": "system",
}


def tool_group(spec: ToolSpec) -> str:
    """Group of a tool, from its name prefix (sonos_* -> smart_home, files_* -> files ...)."""
    return _PREFIX_GROUP.get(spec.name.split("_", 1)[0], spec.category)


def detect_groups(text: str) -> set[str]:
    return {g for g, pat in _GROUP_PATTERNS.items() if pat.search(text)}


def select_tools(text: str, tools: list[ToolSpec]) -> tuple[list[ToolSpec], set[str] | None]:
    """(tools to offer, groups recognised). None groups = nothing recognised, all tools offered."""
    groups = detect_groups(text)
    if not groups:
        return tools, None
    wanted = groups | {ALWAYS}
    return [t for t in tools if tool_group(t) in wanted], groups
