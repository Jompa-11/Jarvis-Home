from jarvis.config import ModelPrice
from jarvis.providers.base import Usage
from jarvis.providers.usage import UsageTracker, estimate_cost


def test_cost_estimate_default_multipliers():
    price = ModelPrice(input_per_mtok=4.0, output_per_mtok=20.0)
    cost = estimate_cost(Usage(input_tokens=1_000_000, output_tokens=100_000, cache_read_tokens=1_000_000), price)
    assert abs(cost - (4.0 + 2.0 + 0.4)) < 1e-9
    assert estimate_cost(Usage(10, 10), None) is None


def test_verified_default_prices():
    from jarvis.config import _default_pricing

    p = _default_pricing()
    # Opus 5.5: cache reads are 0.05x input ($0.20), writes $5 (verified 2026-10-12).
    cost = estimate_cost(Usage(input_tokens=1_000_000, output_tokens=1_000_000, cache_read_tokens=1_000_000, cache_write_tokens=1_000_000), p["claude-opus-5-5"])
    assert abs(cost - (4.0 + 20.0 + 0.20 + 5.0)) < 1e-9
    assert abs(estimate_cost(Usage(cache_read_tokens=1_000_000), p["claude-sonnet-5-5"]) - 0.10) < 1e-9


def test_haiku_long_prompt_tier():
    from jarvis.config import _default_pricing

    haiku = _default_pricing()["claude-haiku-5-5"]
    short = estimate_cost(Usage(input_tokens=100_000, output_tokens=1_000_000), haiku)
    assert abs(short - (0.01 + 0.50)) < 1e-9  # exactly 100K is still the lower tier
    # Over 100K counting cache reads too: the whole request uses the higher prices.
    long = estimate_cost(Usage(input_tokens=60_000, cache_read_tokens=60_000, output_tokens=1_000_000), haiku)
    assert abs(long - (60_000 * 0.50 + 60_000 * 0.05 + 1_000_000 * 2.50) / 1_000_000) < 1e-9


def test_tracker_persists_and_enforces_budget(tmp_path):
    path = tmp_path / "usage.json"
    t = UsageTracker(path, {"m": ModelPrice(input_per_mtok=1000, output_per_mtok=1000)}, monthly_budget_usd=1.0)
    t.record("anthropic", "m", Usage(input_tokens=600, output_tokens=600))
    assert t.budget_exceeded()
    again = UsageTracker(path, {}, monthly_budget_usd=1.0)
    assert again.summary()["requests"] == 1
    assert "content" not in path.read_text()
