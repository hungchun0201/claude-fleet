"""Cost estimation + cumulative token totals."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from core import pricing, transcripts


# ---------- pricing.cost ----------

@pytest.mark.unit
def test_cost_input_and_output():
    totals = {"claude-opus-4-8": {"input": 1_000_000, "output": 1_000_000,
                                  "cache_read": 0, "cache_write_5m": 0, "cache_write_1h": 0}}
    out = pricing.cost(totals)
    assert out["usd"] == 30.0   # 1M*$5 + 1M*$25
    assert out["input"] == 1_000_000 and out["output"] == 1_000_000


@pytest.mark.unit
def test_cost_cache_multipliers():
    totals = {"claude-opus-4-8": {"input": 0, "output": 0,
                                  "cache_read": 1_000_000,        # 1M*$5*0.1  = 0.50
                                  "cache_write_5m": 1_000_000,    # 1M*$5*1.25 = 6.25
                                  "cache_write_1h": 1_000_000}}   # 1M*$5*2.0  = 10.0
    out = pricing.cost(totals)
    assert out["usd"] == 16.75
    assert out["cache_write"] == 2_000_000 and out["cache_read"] == 1_000_000


@pytest.mark.unit
def test_cost_sums_models():
    totals = {
        "claude-opus-4-8": {"input": 1_000_000, "output": 0, "cache_read": 0, "cache_write_5m": 0, "cache_write_1h": 0},
        "claude-haiku-4-5": {"input": 1_000_000, "output": 0, "cache_read": 0, "cache_write_5m": 0, "cache_write_1h": 0},
    }
    assert pricing.cost(totals)["usd"] == 6.0  # $5 + $1


@pytest.mark.unit
def test_cost_unknown_model_unpriced():
    assert pricing.cost({"mystery": {"input": 1_000_000, "output": 0, "cache_read": 0,
                                     "cache_write_5m": 0, "cache_write_1h": 0}}) is None


@pytest.mark.unit
def test_cost_prefix_fallback():
    # An unknown opus point release still prices at the opus rate.
    totals = {"claude-opus-4-8-future": {"input": 1_000_000, "output": 0, "cache_read": 0,
                                         "cache_write_5m": 0, "cache_write_1h": 0}}
    assert pricing.cost(totals)["usd"] == 5.0


# ---------- session_token_totals ----------

def _assistant(model, usage):
    return {"type": "assistant", "message": {"role": "assistant", "model": model, "usage": usage}}


@pytest.mark.unit
def test_totals_accumulate_and_split_cache_ttl(tmp_path):
    rows = [
        _assistant("claude-opus-4-8", {"input_tokens": 100, "output_tokens": 50,
                                       "cache_read_input_tokens": 1000,
                                       "cache_creation_input_tokens": 600,
                                       "cache_creation": {"ephemeral_5m_input_tokens": 200,
                                                          "ephemeral_1h_input_tokens": 400}}),
        _assistant("claude-opus-4-8", {"input_tokens": 10, "output_tokens": 5,
                                       "cache_creation_input_tokens": 0}),
        {"type": "assistant", "message": {"role": "assistant", "model": "<synthetic>",
                                          "usage": {"input_tokens": 9, "output_tokens": 9}}},
    ]
    p = tmp_path / "t.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    t = transcripts.session_token_totals(str(p))["claude-opus-4-8"]
    assert t["input"] == 110 and t["output"] == 55 and t["cache_read"] == 1000
    assert t["cache_write_5m"] == 200 and t["cache_write_1h"] == 400
    assert "<synthetic>" not in transcripts.session_token_totals(str(p))


@pytest.mark.unit
def test_totals_no_ttl_split_falls_back_to_5m(tmp_path):
    rows = [_assistant("claude-opus-4-8", {"input_tokens": 0, "output_tokens": 0,
                                           "cache_creation_input_tokens": 500})]
    p = tmp_path / "t.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    t = transcripts.session_token_totals(str(p))["claude-opus-4-8"]
    assert t["cache_write_5m"] == 500 and t["cache_write_1h"] == 0
