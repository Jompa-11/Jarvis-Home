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

# Cache pricing multipliers relative to the base input price (5-minute cache).
CACHE_READ_FACTOR = 0.1
CACHE_WRITE_FACTOR = 1.25


def estimate_cost(usage: Usage, price: ModelPrice | None) -> float | None:
    if price is None:
        return None
    per_in = price.input_per_mtok / 1_000_000
    per_out = price.output_per_mtok / 1_000_000
    return (
        usage.input_tokens * per_in
        + usage.cache_read_tokens * per_in * CACHE_READ_FACTOR
        + usage.cache_write_tokens * per_in * CACHE_WRITE_FACTOR
        + usage.output_tokens * per_out
    )


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
