from jarvis.config import ModelPrice
from jarvis.providers.base import Usage
from jarvis.providers.usage import UsageTracker, estimate_cost


def test_cost_estimate():
    price = ModelPrice(input_per_mtok=4.0, output_per_mtok=20.0)
    cost = estimate_cost(Usage(input_tokens=1_000_000, output_tokens=100_000, cache_read_tokens=1_000_000), price)
    assert abs(cost - (4.0 + 2.0 + 0.4)) < 1e-9
    assert estimate_cost(Usage(10, 10), None) is None


def test_tracker_persists_and_enforces_budget(tmp_path):
    path = tmp_path / "usage.json"
    t = UsageTracker(path, {"m": ModelPrice(input_per_mtok=1000, output_per_mtok=1000)}, monthly_budget_usd=1.0)
    t.record("anthropic", "m", Usage(input_tokens=600, output_tokens=600))
    assert t.budget_exceeded()
    again = UsageTracker(path, {}, monthly_budget_usd=1.0)
    assert again.summary()["requests"] == 1
    assert "content" not in path.read_text()
