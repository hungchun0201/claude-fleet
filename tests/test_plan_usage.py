"""Plan-usage shaping and cache/backoff logic (no network/keychain)."""
from __future__ import annotations

import pytest

from core import plan_usage


@pytest.fixture(autouse=True)
def _reset_cache():
    plan_usage._cache.update(ts=0.0, data=None, ok=False)
    yield
    plan_usage._cache.update(ts=0.0, data=None, ok=False)


@pytest.mark.unit
def test_win_normalizes_and_rejects_empty():
    assert plan_usage._win({"utilization": 12.0, "resets_at": "2026-06-14T01:50:00Z"}) == \
        {"pct": 12.0, "resets_at": "2026-06-14T01:50:00Z"}
    assert plan_usage._win({"utilization": None}) is None
    assert plan_usage._win(None) is None
    assert plan_usage._win({"utilization": "bad"}) is None


@pytest.mark.unit
def test_shape_maps_real_response():
    raw = {
        "five_hour": {"utilization": 12.0, "resets_at": "a"},
        "seven_day": {"utilization": 3.0, "resets_at": "b"},
        "seven_day_opus": None,
        "seven_day_sonnet": {"utilization": 0.0, "resets_at": "c"},
    }
    out = plan_usage._shape(raw)
    assert out["five_hour"] == {"pct": 12.0, "resets_at": "a"}
    assert out["seven_day"]["pct"] == 3.0
    assert out["seven_day_sonnet"]["pct"] == 0.0
    assert out["seven_day_opus"] is None


@pytest.mark.unit
def test_refresh_caches_within_ttl(monkeypatch):
    calls = {"n": 0}

    def fake_fetch():
        calls["n"] += 1
        return {"five_hour": {"pct": 5.0, "resets_at": "x"}}

    monkeypatch.setattr(plan_usage, "_fetch", fake_fetch)
    monkeypatch.setenv("CLAUDE_FLEET_PLAN_USAGE", "1")
    first = plan_usage.refresh()
    second = plan_usage.refresh()           # within TTL → cached, no 2nd fetch
    assert first == second
    assert calls["n"] == 1
    assert plan_usage.cached() == first


@pytest.mark.unit
def test_refresh_backoff_then_retry(monkeypatch):
    calls = {"n": 0}
    monkeypatch.setattr(plan_usage, "_fetch", lambda: (calls.__setitem__("n", calls["n"] + 1) or None))
    monkeypatch.setenv("CLAUDE_FLEET_PLAN_USAGE", "1")
    assert plan_usage.refresh() is None        # failure → ok stays False
    assert calls["n"] == 1
    # Simulate the fail-TTL having elapsed; next refresh retries.
    plan_usage._cache["ts"] -= plan_usage._FAIL_TTL_S + 1
    plan_usage.refresh()
    assert calls["n"] == 2


@pytest.mark.unit
def test_refresh_disabled(monkeypatch):
    monkeypatch.setenv("CLAUDE_FLEET_PLAN_USAGE", "0")
    monkeypatch.setattr(plan_usage, "_fetch", lambda: pytest.fail("should not fetch when disabled"))
    assert plan_usage.refresh() is None
