"""Estimate USD cost from transcript token usage.

Per-million-token list prices (input / output) come from the Claude model
catalog; prompt-caching uses the standard multipliers — cache write = 1.25x
input (5-min TTL) or 2x input (1-hour TTL), cache read = 0.1x input.

These are pay-as-you-go API *list* prices, NOT what a Pro/Max subscription
actually costs (a subscription bundles usage for a flat fee). Read the number as
"what this session would cost on the metered API" — a useful magnitude, not your
real spend. Override a rate or add a model via CLAUDE_FLEET_PRICING isn't wired;
edit _RATES if list prices change.
"""
from __future__ import annotations

# (input, output) USD per 1M tokens.
_RATES = {
    "claude-fable-5":    (10.0, 50.0),
    "claude-mythos-5":   (10.0, 50.0),
    "claude-opus-4-8":   (5.0, 25.0),
    "claude-opus-4-7":   (5.0, 25.0),
    "claude-opus-4-6":   (5.0, 25.0),
    "claude-opus-4-5":   (5.0, 25.0),
    "claude-sonnet-4-6": (3.0, 15.0),
    "claude-sonnet-4-5": (3.0, 15.0),
    "claude-haiku-4-5":  (1.0, 5.0),
}
_CACHE_WRITE_5M = 1.25
_CACHE_WRITE_1H = 2.0
_CACHE_READ = 0.1
_PER = 1_000_000


def _rate(model: str):
    if model in _RATES:
        return _RATES[model]
    for prefix, rate in _RATES.items():  # tolerate unknown point releases
        if model.startswith(prefix):
            return rate
    return None


def cost(totals_by_model: dict) -> dict | None:
    """`totals_by_model`: {model: {input, cache_read, output, cache_write_5m,
    cache_write_1h}}. Returns {usd, input, cache_write, cache_read, output} —
    summed token counts plus the estimated USD — or None when no model priced."""
    usd = 0.0
    inp = cw = cr = out = 0
    priced = False
    for model, t in totals_by_model.items():
        inp += t.get("input", 0)
        cr += t.get("cache_read", 0)
        out += t.get("output", 0)
        cw += t.get("cache_write_5m", 0) + t.get("cache_write_1h", 0)
        rate = _rate(model)
        if not rate:
            continue
        priced = True
        r_in, r_out = rate
        usd += (
            t.get("input", 0) * r_in
            + t.get("cache_write_5m", 0) * r_in * _CACHE_WRITE_5M
            + t.get("cache_write_1h", 0) * r_in * _CACHE_WRITE_1H
            + t.get("cache_read", 0) * r_in * _CACHE_READ
            + t.get("output", 0) * r_out
        ) / _PER
    if not priced:
        return None
    return {
        "usd": round(usd, 2),
        "input": inp, "cache_write": cw, "cache_read": cr, "output": out,
    }
