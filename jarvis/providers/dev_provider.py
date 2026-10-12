"""Development fallback provider.

This is NOT an AI model. It is a small, deterministic, rule-based intent
matcher (English and Swedish) that exercises the real pipeline — tool
validation, permissions, confirmation, dry-run and adapters — on machines that
have neither a cloud API key nor a local model. Every reply it produces is
labelled as coming from the development fallback.
"""

from __future__ import annotations

import json
import re
import uuid

from ..core.tools import ToolSpec
from .base import AIProvider, ChatMessage, ProviderHealth, ProviderResponse, ToolCall, Usage

LABEL = "[Dev fallback — no AI model configured]"


def _call(name: str, **args) -> ToolCall:
    return ToolCall(id=f"dev_{uuid.uuid4().hex[:12]}", name=name, arguments=args)


def _target(text: str) -> str | None:
    m = re.search(r"\b(?:in|on|i|på)\s+(?:the\s+)?([\wåäöÅÄÖ][\wåäöÅÄÖ .'-]{0,40}?)\s*(?:speaker|högtalaren?)?[.!?]?$", text, re.I)
    return m.group(1).strip() if m else None


def match_intent(text: str, available: set[str]) -> ToolCall | None:
    t = text.strip().lower()
    target = _target(text)

    def ok(name: str) -> bool:
        return name in available

    if re.search(r"\b(speakers?|högtalare|sonos)\b", t) and re.search(r"\b(list|which|what|show|vilka|visa|lista)\b", t):
        return _call("sonos_list_speakers") if ok("sonos_list_speakers") else None
    if re.search(r"\b(favou?rites?|favoriter)\b", t) and not re.search(r"\b(play|spela)\b", t):
        return _call("sonos_list_favorites") if ok("sonos_list_favorites") else None
    m = re.search(r"\b(?:volume|volym)\D{0,12}(\d{1,3})\b", t)
    if m and ok("sonos_set_volume") and target:
        return _call("sonos_set_volume", speaker=target, level=min(100, int(m.group(1))))
    for words, action in (
        (r"\b(pause|pausa)\b", "pause"),
        (r"\b(resume|play|fortsätt|spela)\b(?!.*\b(favou?rite|favorit))", "play"),
        (r"\b(stop|stoppa)\b", "stop"),
        (r"\b(next|nästa)\b", "next"),
    ):
        if re.search(words, t) and ok("sonos_playback") and target:
            return _call("sonos_playback", speaker=target, action=action)
    if re.search(r"\b(playing|spelas|playback status)\b", t) and ok("sonos_get_playback") and target:
        return _call("sonos_get_playback", speaker=target)
    m = re.search(r"\b(?:open|start|launch|öppna|starta)\s+([\w-]+)", t)
    if m and ok("computer_open_app"):
        return _call("computer_open_app", app=m.group(1))
    if re.search(r"\b(windows|fönster)\b", t) and ok("computer_list_windows"):
        return _call("computer_list_windows")
    if re.search(r"\b(status|systems?|system)\b", t) and ok("system_get_status"):
        return _call("system_get_status")
    if re.search(r"\b(time|date|klockan|tid|datum)\b", t) and ok("system_get_time"):
        return _call("system_get_time")
    return None


def _summarize(tool_name: str | None, content: str, is_error: bool) -> str:
    try:
        payload = json.loads(content)
    except (json.JSONDecodeError, TypeError):
        payload = {"data": content}
    if is_error or not payload.get("ok", True):
        return f"{LABEL}\nThe action did not succeed: {payload.get('error') or content}"
    data = payload.get("data")
    prefix = "DRY RUN — " if payload.get("dry_run") else ""
    if tool_name == "sonos_list_speakers" and isinstance(data, dict):
        speakers = data.get("speakers", [])
        if not speakers:
            return f"{LABEL}\nNo Sonos speakers were found in Home Assistant."
        lines = [f"- {s.get('name')} ({s.get('area') or 'no room'}) — {s.get('state')}" for s in speakers]
        return f"{LABEL}\nI found {len(speakers)} speaker(s):\n" + "\n".join(lines)
    if tool_name == "system_get_time" and isinstance(data, dict):
        return f"{LABEL}\nIt is {data.get('local_time')}."
    verified = payload.get("verified")
    check = " (verified on the device)" if verified is True else " (could not be verified)" if verified is False else ""
    if tool_name == "sonos_set_volume" and isinstance(data, dict):
        if payload.get("dry_run"):
            return f"{LABEL}\n{prefix}would set {data.get('speaker')} to {data['would_call'].get('volume_level', 0) * 100:.0f}%."
        return f"{LABEL}\n{data.get('speaker')} volume is now {data.get('volume')}%{check}."
    if tool_name == "sonos_playback" and isinstance(data, dict):
        if payload.get("dry_run"):
            return f"{LABEL}\n{prefix}would call {data['would_call']['service']} on {data.get('speaker')}."
        return f"{LABEL}\n{data.get('speaker')}: {data.get('action')} sent, state is now {data.get('state') or 'unknown'}{check}."
    if tool_name == "computer_open_app" and isinstance(data, dict):
        if payload.get("dry_run"):
            return f"{LABEL}\n{prefix}would open {data.get('app')} ({' '.join(data.get('would_run', []))})."
        return f"{LABEL}\nOpened {data.get('app')}."
    return f"{LABEL}\n{prefix}{tool_name} completed:\n{json.dumps(data, ensure_ascii=False, indent=2)[:1500]}"


class DevProvider(AIProvider):
    name = "dev"
    kind = "dev"

    async def health(self) -> ProviderHealth:
        return ProviderHealth(True, "Rule-based development fallback (not an AI model).")

    async def complete(self, *, system, messages, tools: list[ToolSpec], model, effort, timeout_s) -> ProviderResponse:
        last = messages[-1] if messages else None
        available = {t.name for t in tools}
        if last is not None and last.role == "tool":
            # Summarize the results of the tool calls from the previous step.
            results = []
            for m in reversed(messages):
                if m.role != "tool":
                    break
                results.append(_summarize(m.tool_name, m.content, m.is_error))
            text = "\n\n".join(reversed(results))
            return self._reply(text, [])
        text = last.content if last is not None and last.role == "user" else ""
        call = match_intent(text, available)
        if call is not None:
            return self._reply("", [call])
        examples = [
            ex
            for tool, ex in (
                ("sonos_list_speakers", '"list speakers"'),
                ("sonos_set_volume", '"volume 30 in kitchen"'),
                ("sonos_playback", '"pause in living room"'),
                ("computer_open_app", '"open notepad"'),
                ("system_get_status", '"system status"'),
            )
            if tool in available
        ]
        return self._reply(
            f"{LABEL}\nI can only handle simple commands in this mode, for example: "
            f"{', '.join(examples)}. Configure Claude or Ollama for natural conversation.",
            [],
        )

    def _reply(self, text: str, calls: list[ToolCall]) -> ProviderResponse:
        return ProviderResponse(
            text=text,
            tool_calls=calls,
            stop_reason="tool_use" if calls else "end_turn",
            usage=Usage(),
            provider=self.name,
            model="rules-v1",
        )
