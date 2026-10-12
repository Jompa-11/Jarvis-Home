"""Turn a chat reply into text that sounds right when read aloud.

The model is asked to answer in plain spoken language when a reply will be
read aloud, but replies can still contain markdown, lists, links, emojis or
code. This module removes those before the text reaches the speech engine.
"""

from __future__ import annotations

import re

_CODE_BLOCK = re.compile(r"```.*?(```|$)", re.DOTALL)
_INLINE_CODE = re.compile(r"`([^`]*)`")
_LINK = re.compile(r"\[([^\]]+)\]\((?:[^)]+)\)")
_URL = re.compile(r"https?://\S+|www\.\S+")
_HEADING = re.compile(r"^\s{0,3}#{1,6}\s*", re.MULTILINE)
_BULLET = re.compile(r"^\s*(?:[-*+•]|\d+[.)])\s+", re.MULTILINE)
_EMPHASIS = re.compile(r"(\*\*|__|\*|_|~~)(?=\S)(.+?)(?<=\S)\1")
_TABLE_RULE = re.compile(r"^\s*\|?[\s:|-]+\|[\s:|-]*$", re.MULTILINE)
_LABEL = re.compile(r"^\[Dev fallback[^\]]*\]\s*")
# Emoji and pictographs, dingbats, variation selectors, joiners and flags.
_EMOJI = re.compile(
    "[\U0001f000-\U0001faff\U00002600-\U000027bf\U0001f1e6-\U0001f1ff\U00002b00-\U00002bff️‍⃣]+"
)
_SENTENCE_END = re.compile(r"(?<=[.!?…])\s+(?=[A-ZÅÄÖ0-9\"'(])")


def clean_for_speech(text: str) -> str:
    """Plain sentences only: no markdown, lists, links, emojis or code."""
    t = _LABEL.sub("", text or "")
    t = _CODE_BLOCK.sub(" ", t)
    t = _INLINE_CODE.sub(r"\1", t)
    t = _LINK.sub(r"\1", t)
    t = _URL.sub("", t)
    t = _TABLE_RULE.sub("", t)
    t = _HEADING.sub("", t)
    t = _EMOJI.sub("", t)
    for _ in range(2):  # nested emphasis such as ***bold italic***
        t = _EMPHASIS.sub(r"\2", t)
    lines = []
    for raw in t.splitlines():
        is_item = bool(_BULLET.match(raw))
        line = _BULLET.sub("", raw).replace("|", ", ").strip(" ,")
        if not line:
            continue
        # A list item becomes its own sentence, so the voice pauses between items.
        if is_item and line[-1] not in ".!?:;…":
            line += "."
        lines.append(line)
    t = " ".join(lines)
    t = re.sub(r"[*_#>]+", " ", t)
    t = re.sub(r"\s+([,.!?;:])", r"\1", t)
    return re.sub(r"\s{2,}", " ", t).strip()


def split_sentences(text: str, max_chars: int = 240) -> list[str]:
    """Split into sentences so speech can start early and stop between them."""
    parts = [p.strip() for p in _SENTENCE_END.split(text) if p.strip()]
    out: list[str] = []
    for p in parts:
        while len(p) > max_chars:
            cut = p.rfind(",", 0, max_chars)
            cut = cut if cut > max_chars // 3 else p.rfind(" ", 0, max_chars)
            cut = cut if cut > 0 else max_chars
            out.append(p[: cut + 1].strip())
            p = p[cut + 1 :].strip()
        if p:
            out.append(p)
    return out


_SV_WORDS = re.compile(r"\b(och|jag|det|är|inte|du|vad|kan|har|en|ett|som|på|med|för|att|klockan|nu)\b", re.IGNORECASE)
_EN_WORDS = re.compile(r"\b(the|and|is|are|you|what|can|have|it|to|of|in|for|with|now|time)\b", re.IGNORECASE)


def guess_language(text: str) -> str:
    """'sv' or 'en' for choosing a voice, from letters and common words."""
    sv = len(_SV_WORDS.findall(text)) + 2 * len(re.findall(r"[åäöÅÄÖ]", text))
    en = len(_EN_WORDS.findall(text))
    return "en" if en > sv else "sv"
