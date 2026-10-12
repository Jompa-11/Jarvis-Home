"""Claude (Anthropic API) provider — the intended production cloud model."""

from __future__ import annotations

import json
import logging
from typing import Any

import anthropic

from ..config import AnthropicConfig
from ..core.tools import ToolSpec
from ..secrets import get_secret
from .base import AIProvider, ChatMessage, ProviderError, ProviderHealth, ProviderResponse, ToolCall, Usage

log = logging.getLogger(__name__)

# Models that accept the server-side refusal fallback ("fallbacks": "default").
_FALLBACK_MODELS = {"claude-opus-5-5", "claude-opus-5", "claude-fable-5-1", "claude-sonnet-5-5"}
_FALLBACK_BETA = "server-side-fallback-2026-07-01"
DEFAULT_MODEL = "claude-opus-5-5"


class AnthropicProvider(AIProvider):
    name = "anthropic"
    kind = "cloud"

    def __init__(self, cfg: AnthropicConfig, client: Any | None = None) -> None:
        self._cfg = cfg
        self._client = client

    def _get_client(self) -> Any:
        if self._client is not None:
            return self._client
        key = get_secret(self._cfg.api_key_env)
        if not key:
            raise ProviderError(f"No API key: set {self._cfg.api_key_env}.", fallback_ok=True)
        self._client = anthropic.AsyncAnthropic(api_key=key, max_retries=self._cfg.max_retries)
        return self._client

    async def health(self) -> ProviderHealth:
        if not self._cfg.enabled:
            return ProviderHealth(False, "Disabled in configuration.")
        if self._client is None and not get_secret(self._cfg.api_key_env):
            return ProviderHealth(False, f"Not configured ({self._cfg.api_key_env} is not set).")
        # Credentials are present. We avoid a paid request just to check health;
        # real failures surface on the first request and are reported then.
        return ProviderHealth(True, "API key configured.")

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
            raise ProviderError("Anthropic provider is disabled.")
        client = self._get_client()
        model = model or DEFAULT_MODEL

        params: dict[str, Any] = {
            "model": model,
            "max_tokens": self._cfg.max_tokens,
            "system": system,
            "messages": to_anthropic_messages(messages),
            # Automatic prompt caching of the stable prefix (tools + system + history).
            "cache_control": {"type": "ephemeral"},
            "timeout": timeout_s,
        }
        if tools:
            params["tools"] = [
                {"name": t.name, "description": t.description, "input_schema": t.parameters} for t in tools
            ]
        if effort:
            params["output_config"] = {"effort": effort}
        betas: list[str] = []
        if self._cfg.server_side_fallback and model in _FALLBACK_MODELS:
            betas.append(_FALLBACK_BETA)
            params["fallbacks"] = "default"
        if betas:
            params["betas"] = betas

        try:
            resp = await client.beta.messages.create(**params)
        except anthropic.AuthenticationError as exc:
            raise ProviderError("Anthropic rejected the API key.", fallback_ok=True) from exc
        except anthropic.PermissionDeniedError as exc:
            raise ProviderError("The API key lacks permission for this model.", fallback_ok=True) from exc
        except anthropic.NotFoundError as exc:
            raise ProviderError(f"Model '{model}' not found.", fallback_ok=True) from exc
        except anthropic.RateLimitError as exc:
            raise ProviderError("Anthropic rate limit reached.", retryable=True) from exc
        except anthropic.BadRequestError as exc:
            raise ProviderError(f"Anthropic rejected the request: {exc.message}", fallback_ok=True) from exc
        except anthropic.APITimeoutError as exc:
            raise ProviderError("Anthropic request timed out.", retryable=True) from exc
        except anthropic.APIStatusError as exc:
            raise ProviderError(f"Anthropic API error ({exc.status_code}).", retryable=exc.status_code >= 500) from exc
        except anthropic.APIConnectionError as exc:
            raise ProviderError("Cannot reach the Anthropic API (network).", retryable=True) from exc

        text_parts: list[str] = []
        calls: list[ToolCall] = []
        for block in resp.content:
            if block.type == "text":
                text_parts.append(block.text)
            elif block.type == "tool_use":
                args = block.input if isinstance(block.input, dict) else _parse_json(block.input)
                calls.append(ToolCall(id=block.id, name=block.name, arguments=args))

        stop = resp.stop_reason or "end_turn"
        text = "".join(text_parts).strip()
        if stop == "refusal":
            text = text or "I can't help with that request."
            calls = []
        elif stop == "max_tokens" and not calls:
            text = (text + "\n\n[Response truncated: output limit reached.]").strip()

        u = resp.usage
        usage = Usage(
            input_tokens=getattr(u, "input_tokens", 0) or 0,
            output_tokens=getattr(u, "output_tokens", 0) or 0,
            cache_read_tokens=getattr(u, "cache_read_input_tokens", 0) or 0,
            cache_write_tokens=getattr(u, "cache_creation_input_tokens", 0) or 0,
        )
        raw = [b.to_dict(mode="json") for b in resp.content]
        return ProviderResponse(
            text=text,
            tool_calls=calls,
            stop_reason=stop,
            usage=usage,
            provider=self.name,
            model=getattr(resp, "model", model) or model,
            raw_assistant=raw,
        )

    async def aclose(self) -> None:
        if self._client is not None and hasattr(self._client, "close"):
            await self._client.close()


def _parse_json(value: Any) -> dict[str, Any]:
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}


def to_anthropic_messages(messages: list[ChatMessage]) -> list[dict[str, Any]]:
    """Translate neutral messages into Messages API format.

    Assistant turns produced by Claude are replayed exactly as returned
    (including thinking blocks). Consecutive tool results are grouped into one
    user message, as the API expects.
    """
    out: list[dict[str, Any]] = []
    pending_results: list[dict[str, Any]] = []

    def flush_results() -> None:
        if pending_results:
            out.append({"role": "user", "content": list(pending_results)})
            pending_results.clear()

    for m in messages:
        if m.role == "tool":
            pending_results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": m.tool_call_id,
                    "content": m.content,
                    **({"is_error": True} if m.is_error else {}),
                }
            )
            continue
        flush_results()
        if m.role == "user":
            out.append({"role": "user", "content": m.model_text()})
        elif m.role == "assistant":
            raw = m.raw.get("anthropic")
            if raw:
                out.append({"role": "assistant", "content": raw})
            else:
                blocks: list[dict[str, Any]] = []
                if m.content:
                    blocks.append({"type": "text", "text": m.content})
                for c in m.tool_calls:
                    blocks.append({"type": "tool_use", "id": c.id, "name": c.name, "input": c.arguments})
                if blocks:
                    out.append({"role": "assistant", "content": blocks})
    flush_results()
    return out
