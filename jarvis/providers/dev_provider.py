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


_FILE_WORDS = {
    "faktura": "faktura", "fakturan": "faktura", "fakturor": "faktura", "invoice": "invoice", "invoices": "invoice",
    "kvitto": "kvitto", "kvittot": "kvitto", "kvitton": "kvitto", "receipt": "receipt", "receipts": "receipt",
    "avtal": "avtal", "avtalet": "avtal", "kontrakt": "kontrakt", "kontraktet": "kontrakt", "contract": "contract",
}


def _file_search(text: str) -> dict | None:
    """Arguments for files_search, or None when the text is not a file search."""
    t = text.strip().lower()
    m = re.search(
        r"\b(?:sök|leta|hitta|search|find|look)\b(?:\s+(?:efter|upp|for))?\s+(?:the\s+)?(?:filen|file)\s+(?:som heter\s+|named\s+|called\s+)?(.+)$",
        t,
    )
    if m:
        query = m.group(1).strip(" \"'`.!?")
    elif re.search(r"\b(hitta|sök|leta|visa|find|search|show)\b", t):
        word = next((w for w in re.findall(r"[\wåäö]+", t) if w in _FILE_WORDS), None)
        if word is None:
            return None
        query = _FILE_WORDS[word]
    else:
        return None
    args: dict = {"query": query}
    if re.search(r"\b(senaste veckan|förra veckan|denna vecka|last week|this week|past week)\b", t):
        args["period"] = "week"
    elif re.search(r"\b(idag|i dag|today)\b", t):
        args["period"] = "today"
    elif re.search(r"\b(senaste månaden|last month|this month)\b", t):
        args["period"] = "month"
    m = re.search(r"\b(?:som|as|type)\s+(pdf|docx?|xlsx?|pptx?|jpe?g|png|zip)\b", t)
    if m:
        args["file_type"] = m.group(1)
    return args


def match_intent(text: str, available: set[str]) -> ToolCall | None:
    t = text.strip().lower()
    target = _target(text)

    def ok(name: str) -> bool:
        return name in available

    if re.search(r"\b(sammanfatta|sammanfattning|summari[sz]e|summary)\b", t) and ok("monitor_summary_now"):
        return _call("monitor_summary_now")
    if ok("files_search") and (args := _file_search(text)) is not None:
        return _call("files_search", **args)

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
    if re.search(r"\b(which|what|vilka)\b.*\b(apps?|applications?|programs?|program|appar)\b", t) and ok("computer_list_apps"):
        return _call("computer_list_apps")
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
    if tool_name == "system_get_status" and isinstance(data, dict):
        lines = []
        for p in data.get("ai_providers", []):
            lines.append(f"- AI {p.get('name')}: {'available' if p.get('available') else p.get('detail')}")
        lines.append(f"- Home Assistant: {str(data.get('home_assistant', 'unknown')).replace('_', ' ')}")
        lines.append(f"- Sonos speakers: {data.get('sonos_speakers', 0)}")
        lines.append(f"- Microphone: {str(data.get('microphone')).replace('_', ' ')}; camera: {str(data.get('camera')).replace('_', ' ')}")
        dry = data.get("dry_run", {})
        lines.append(f"- Dry-run: computer {'on' if dry.get('computer') else 'off'}, smart home {'on' if dry.get('smart_home') else 'off'}")
        return f"{LABEL}\nSystem status:\n" + "\n".join(lines)
    if tool_name == "files_search" and isinstance(data, dict):
        matches = data.get("matches", [])
        if not matches:
            return f"{LABEL}\n{data.get('note') or 'No matching files.'} {data.get('scope', '')}".rstrip()
        lines = [f"- {m['name']} ({m['folder']}, {m['modified']})" for m in matches[:10]]
        return f"{LABEL}\nI found {len(matches)} file(s), newest first:\n" + "\n".join(lines) + f"\n{data.get('scope', '')}"
    if tool_name == "monitor_summary_now" and isinstance(data, dict):
        lines = [data.get("title") or "Summary."]
        lines += [f"- {m}" for m in data.get("mail") or []]
        files = data.get("files") or {}
        if files.get("count"):
            lines.append(f"- New files ({files['count']}): {', '.join(files.get('top', []))}")
        lines += [f"- {w['time']} {w['title']}" for w in data.get("waited") or []]
        return f"{LABEL}\n" + "\n".join(lines)
    if tool_name == "files_open_folder" and isinstance(data, dict):
        if payload.get("dry_run"):
            return f"{LABEL}\n{prefix}would open the folder {data.get('folder')} with {data.get('file')} highlighted."
        return f"{LABEL}\nOpened the folder {data.get('folder')} with {data.get('file')} highlighted."
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


def _open_folder_from_history(text: str, messages: list[ChatMessage], available: set[str]) -> ToolCall | None:
    """"öppna mappen där den ligger" right after a files_search: the newest match shown."""
    if "files_open_folder" not in available:
        return None
    if not re.search(r"\b(öppna|visa|open|show)\b.*\b(mappen|folder|katalogen)\b", text.lower()):
        return None
    for m in reversed(messages):
        if m.role == "tool" and m.tool_name == "files_search":
            try:
                matches = json.loads(m.content).get("data", {}).get("matches", [])
            except (json.JSONDecodeError, AttributeError, TypeError):
                return None
            return _call("files_open_folder", path=matches[0]["path"]) if matches else None
    return None


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
        call = _open_folder_from_history(text, messages, available) or match_intent(text, available)
        if call is not None:
            return self._reply("", [call])
        examples = [
            ex
            for tool, ex in (
                ("sonos_list_speakers", '"list speakers"'),
                ("sonos_set_volume", '"volume 30 in kitchen"'),
                ("sonos_playback", '"pause in living room"'),
                ("computer_open_app", '"open notepad"'),
                ("files_search", '"hitta min senaste faktura"'),
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
