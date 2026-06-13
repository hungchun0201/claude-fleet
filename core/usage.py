"""Aggregate token usage across recent transcripts for the navbar.

Two numbers sit next to the busy/idle counts:
  - window_tokens: billable tokens (input + cache_creation + output) used in the
    last WINDOW_HOURS across all sessions — a proxy for Claude's rolling
    5-hour rate-limit window. cache_read is intentionally excluded: it is the
    per-turn re-read of the existing context and would inflate the total 50-100×.
  - by_model: the same metric grouped by model id.

Only transcripts whose mtime falls inside the window are scanned, and the
result is cached for CACHE_TTL_S so the 2-second snapshot loop stays cheap.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Optional

from core import sessions
from core.transcripts import _parse_ts_ms

WINDOW_HOURS = 5
CACHE_TTL_S = 60

# Anthropic does not expose the rolling 5-hour token cap as a readable number,
# so the percentage is computed against a configurable budget. Override with the
# CLAUDE_FLEET_5H_TOKEN_BUDGET env var or ~/.config/claude-fleet/5h-token-budget.
DEFAULT_5H_BUDGET = 53_000_000
_BUDGET_FILE = Path.home() / ".config" / "claude-fleet" / "5h-token-budget"

_cache: dict = {"ts": 0.0, "data": None}


def _budget() -> Optional[int]:
    """Configured 5h token budget, or None to disable the percentage."""
    raw = os.environ.get("CLAUDE_FLEET_5H_TOKEN_BUDGET")
    if raw is None:
        try:
            raw = _BUDGET_FILE.read_text().strip()
        except OSError:
            raw = ""
    if raw:
        try:
            val = int(raw.replace("_", "").replace(",", ""))
            return val if val > 0 else None
        except ValueError:
            pass
    return DEFAULT_5H_BUDGET


def _billable(usage: dict) -> int:
    """New tokens billed this turn — excludes cheap cached context reads."""
    return (
        int(usage.get("input_tokens") or 0)
        + int(usage.get("cache_creation_input_tokens") or 0)
        + int(usage.get("output_tokens") or 0)
    )


def _compute(projects_dir: Path, window_hours: int, now_ms: int) -> dict:
    start_ms = now_ms - window_hours * 3600 * 1000
    cutoff_s = start_ms / 1000
    total = 0
    by_model: dict[str, int] = {}

    if not projects_dir.exists():
        return {"window_hours": window_hours, "window_tokens": 0, "by_model": {}}

    for f in projects_dir.glob("*/*.jsonl"):
        try:
            if f.stat().st_mtime < cutoff_s:
                continue
        except OSError:
            continue
        try:
            with f.open() as fh:
                for line in fh:
                    # Cheap pre-filter: skip the ~95% of rows with no usage block
                    # before paying for json.loads.
                    if '"usage"' not in line:
                        continue
                    try:
                        d = json.loads(line)
                    except Exception:
                        continue
                    if d.get("type") != "assistant":
                        continue
                    msg = d.get("message") or {}
                    if not isinstance(msg, dict):
                        continue
                    model = msg.get("model") or ""
                    usage = msg.get("usage")
                    if not isinstance(usage, dict) or model in ("", "<synthetic>"):
                        continue
                    ts_ms = _parse_ts_ms(d.get("timestamp") or "")
                    if ts_ms is None or ts_ms < start_ms:
                        continue
                    tok = _billable(usage)
                    total += tok
                    by_model[model] = by_model.get(model, 0) + tok
        except OSError:
            continue

    return {"window_hours": window_hours, "window_tokens": total, "by_model": by_model}


def summary() -> dict:
    """Cached usage summary for the dashboard navbar."""
    now = time.time()
    cached = _cache["data"]
    if cached is not None and (now - _cache["ts"]) < CACHE_TTL_S:
        return cached
    data = _compute(sessions.PROJECTS_DIR, WINDOW_HOURS, int(now * 1000))
    budget = _budget()
    data["window_budget"] = budget
    data["window_pct"] = round(data["window_tokens"] / budget * 100, 1) if budget else None
    _cache["ts"] = now
    _cache["data"] = data
    return data
