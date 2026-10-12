"""Local model provider using Ollama's HTTP API (http://127.0.0.1:11434)."""

from __future__ import annotations

import json
import uuid
from typing import Any

import httpx

from ..config import OllamaConfig
from ..core.tools import ToolSpec
from .base import AIProvider, ChatMessage, ProviderError, ProviderHealth, ProviderResponse, ToolCall, Usage


class OllamaProvider(AIProvider):
    name = "ollama"
    kind = "local"

    def __init__(self, cfg: OllamaConfig, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._cfg = cfg
        self._client = httpx.AsyncClient(base_url=cfg.base_url, transport=transport, timeout=10.0)

    async def health(self) -> ProviderHealth:
        if not self._cfg.enabled:
            return ProviderHealth(False, "Disabled in configuration.")
        try:
            r = await self._client.get("/api/tags", timeout=3.0)
            r.raise_for_status()
            models = [m.get("name", "") for m in r.json().get("models", [])]
        except (httpx.HTTPError, ValueError):
            return ProviderHealth(False, f"Ollama not reachable at {self._cfg.base_url}.")
        if not _has_model(models, self._cfg.model):
            return ProviderHealth(
                False, f"Ollama is running but model '{self._cfg.model}' is not pulled (ollama pull {self._cfg.model}).", models
            )
        return ProviderHealth(True, f"Ollama running, model '{self._cfg.model}' available.", models)

    async def complete(
        self,
        *,
        system: str,
        messages: list[ChatMessage],
        tools: list[ToolSpec],
        model: str | None,
        effort: str | None,
        timeout_s: float,
    ) -> ProviderResponse:
        if not self._cfg.enabled:
            raise ProviderError("Ollama provider is disabled.")
        model = model or self._cfg.model
        body: dict[str, Any] = {
            "model": model,
            "messages": [{"role": "system", "content": system}, *to_ollama_messages(messages)],
            "stream": False,
            "keep_alive": self._cfg.keep_alive,
            "options": {"num_ctx": self._cfg.num_ctx, "temperature": self._cfg.temperature},
        }
        if tools:
            body["tools"] = [
                {"type": "function", "function": {"name": t.name, "description": t.description, "parameters": t.parameters}}
                for t in tools
            ]
        try:
            r = await self._client.post("/api/chat", json=body, timeout=timeout_s)
        except httpx.TimeoutException as exc:
            raise ProviderError("Local model timed out.", retryable=True) from exc
        except httpx.HTTPError as exc:
            raise ProviderError(f"Ollama not reachable at {self._cfg.base_url}.") from exc
        if r.status_code == 404:
            raise ProviderError(f"Ollama model '{model}' not found (ollama pull {model}).")
        if r.status_code >= 400:
            raise ProviderError(f"Ollama error ({r.status_code}).", retryable=r.status_code >= 500)
        data = r.json()
        msg = data.get("message") or {}
        calls = []
        for tc in msg.get("tool_calls") or []:
            fn = tc.get("function") or {}
            args = fn.get("arguments") or {}
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except json.JSONDecodeError:
                    args = {}
            calls.append(ToolCall(id=f"call_{uuid.uuid4().hex[:16]}", name=fn.get("name", ""), arguments=args))
        return ProviderResponse(
            text=(msg.get("content") or "").strip(),
            tool_calls=calls,
            stop_reason="tool_use" if calls else (data.get("done_reason") or "stop"),
            usage=Usage(input_tokens=data.get("prompt_eval_count", 0) or 0, output_tokens=data.get("eval_count", 0) or 0),
            provider=self.name,
            model=model,
        )

    async def aclose(self) -> None:
        await self._client.aclose()


def _has_model(available: list[str], wanted: str) -> bool:
    if wanted in available:
        return True
    # "llama3.2" matches "llama3.2:latest"
    return ":" not in wanted and f"{wanted}:latest" in available


def to_ollama_messages(messages: list[ChatMessage]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for m in messages:
        if m.role == "user":
            out.append({"role": "user", "content": m.model_text()})
        elif m.role == "assistant":
            entry: dict[str, Any] = {"role": "assistant", "content": m.content}
            if m.tool_calls:
                entry["tool_calls"] = [{"function": {"name": c.name, "arguments": c.arguments}} for c in m.tool_calls]
            out.append(entry)
        elif m.role == "tool":
            out.append({"role": "tool", "content": m.content, "tool_name": m.tool_name or ""})
    return out
