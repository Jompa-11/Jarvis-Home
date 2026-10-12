"""Approximate usage and cost tracking with a monthly budget guard.

Only counts are stored (tokens, requests, estimated cost) — never content.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path

from ..config import ModelPrice
from .base import Usage

log = logging.getLogger(__name__)

# Fallback cache multipliers relative to the input price, used when a model's
# price entry does not list cache prices explicitly (5-minute cache).
CACHE_READ_FACTOR = 0.1
CACHE_WRITE_FACTOR = 1.25


def estimate_cost(usage: Usage, price: ModelPrice | None) -> float | None:
    if price is None:
        return None
    prompt_tokens = usage.input_tokens + usage.cache_read_tokens + usage.cache_write_tokens
    if price.long_prompt and price.long_prompt_threshold_tokens and prompt_tokens > price.long_prompt_threshold_tokens:
        price = price.long_prompt
    read = price.cache_read_per_mtok if price.cache_read_per_mtok is not None else price.input_per_mtok * CACHE_READ_FACTOR
    write = price.cache_write_per_mtok if price.cache_write_per_mtok is not None else price.input_per_mtok * CACHE_WRITE_FACTOR
    return (
        usage.input_tokens * price.input_per_mtok
        + usage.cache_read_tokens * read
        + usage.cache_write_tokens * write
        + usage.output_tokens * price.output_per_mtok
    ) / 1_000_000


class UsageTracker:
    def __init__(self, path: Path | None, pricing: dict[str, ModelPrice], monthly_budget_usd: float) -> None:
        self._path = path
        self._pricing = pricing
        self.monthly_budget_usd = monthly_budget_usd
        self._lock = threading.Lock()
        self._data: dict[str, dict] = {}
        if path and path.exists():
            try:
                self._data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                log.warning("Usage file unreadable; starting fresh")

    @staticmethod
    def _month() -> str:
        return time.strftime("%Y-%m")

    def record(self, provider: str, model: str, usage: Usage) -> float | None:
        cost = estimate_cost(usage, self._pricing.get(model))
        with self._lock:
            month = self._data.setdefault(self._month(), {"cost_usd": 0.0, "requests": 0, "by_model": {}})
            entry = month["by_model"].setdefault(
                f"{provider}:{model}",
                {"requests": 0, "input_tokens": 0, "output_tokens": 0, "cache_read_tokens": 0, "cache_write_tokens": 0, "cost_usd": 0.0, "unpriced": False},
            )
            entry["requests"] += 1
            entry["input_tokens"] += usage.input_tokens
            entry["output_tokens"] += usage.output_tokens
            entry["cache_read_tokens"] += usage.cache_read_tokens
            entry["cache_write_tokens"] += usage.cache_write_tokens
            month["requests"] += 1
            if cost is not None:
                entry["cost_usd"] += cost
                month["cost_usd"] += cost
            elif provider == "anthropic":
                entry["unpriced"] = True
            self._save()
        return cost

    def month_cost(self) -> float:
        return float(self._data.get(self._month(), {}).get("cost_usd", 0.0))

    def budget_exceeded(self) -> bool:
        return self.monthly_budget_usd > 0 and self.month_cost() >= self.monthly_budget_usd

    def summary(self) -> dict:
        month = self._data.get(self._month(), {})
        return {
            "month": self._month(),
            "estimated_cost_usd": round(self.month_cost(), 4),
            "budget_usd": self.monthly_budget_usd,
            "requests": month.get("requests", 0),
            "by_model": month.get("by_model", {}),
            "budget_exceeded": self.budget_exceeded(),
        }

    def _save(self) -> None:
        if not self._path:
            return
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._data, indent=2), encoding="utf-8")
            tmp.replace(self._path)
        except OSError:
            log.warning("Could not persist usage data")
