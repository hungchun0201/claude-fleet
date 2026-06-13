"""Per-card model/token readout and the navbar 5h usage aggregator."""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from core import usage
from core.transcripts import last_usage_and_model


def _assistant(model: str, usage_d: dict | None, ts_ms: int | None = None) -> dict:
    row: dict = {
        "type": "assistant",
        "message": {"role": "assistant", "model": model, "stop_reason": "end_turn",
                    "content": [{"type": "text", "text": "x"}]},
    }
    if usage_d is not None:
        row["message"]["usage"] = usage_d
    if ts_ms is not None:
        row["timestamp"] = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).isoformat().replace("+00:00", "Z")
    return row


def _write(p: Path, rows: list[dict]) -> str:
    p.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    return str(p)


# ---------- last_usage_and_model (per-card) ----------

@pytest.mark.unit
def test_last_usage_basic(tmp_path):
    u = {"input_tokens": 100, "cache_read_input_tokens": 74000,
         "cache_creation_input_tokens": 500, "output_tokens": 300}
    tp = _write(tmp_path / "t.jsonl", [_assistant("claude-opus-4-8", u)])
    out = last_usage_and_model(tp)
    assert out["model"] == "claude-opus-4-8"
    assert out["context_tokens"] == 100 + 74000 + 500  # input + cache_read + cache_creation
    assert out["out_tokens"] == 300


@pytest.mark.unit
def test_last_usage_skips_synthetic_tail(tmp_path):
    u = {"input_tokens": 10, "cache_read_input_tokens": 0,
         "cache_creation_input_tokens": 0, "output_tokens": 5}
    real = _assistant("claude-sonnet-4-6", u)
    synthetic = {"type": "assistant", "isApiErrorMessage": True,
                 "message": {"role": "assistant", "model": "<synthetic>",
                             "content": [{"type": "text", "text": "error"}]}}
    tp = _write(tmp_path / "t.jsonl", [real, synthetic])
    out = last_usage_and_model(tp)
    assert out is not None
    assert out["model"] == "claude-sonnet-4-6"


@pytest.mark.unit
def test_last_usage_missing_file():
    assert last_usage_and_model("/no/such/file.jsonl") is None


# ---------- usage._compute (navbar) ----------

def _proj(tmp_path: Path, slug: str, rows: list[dict], name: str = "s.jsonl") -> Path:
    d = tmp_path / slug
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    return tmp_path


@pytest.mark.unit
def test_compute_window_and_by_model(tmp_path):
    now_ms = int(time.time() * 1000)  # real clock: freshly-written files pass the mtime pre-filter
    inwin = now_ms - 60_000           # 1 min ago — inside the 5h window
    u1 = {"input_tokens": 100, "cache_creation_input_tokens": 50,
          "cache_read_input_tokens": 9999, "output_tokens": 200}   # billable = 350
    u2 = {"input_tokens": 10, "cache_creation_input_tokens": 0,
          "cache_read_input_tokens": 0, "output_tokens": 40}       # billable = 50
    root = _proj(tmp_path, "proj-a", [
        _assistant("claude-opus-4-8", u1, ts_ms=inwin),
        _assistant("claude-fable-5", u2, ts_ms=inwin),
    ])
    out = usage._compute(root, window_hours=5, now_ms=now_ms)
    assert out["window_tokens"] == 400                 # cache_read excluded
    assert out["by_model"]["claude-opus-4-8"] == 350
    assert out["by_model"]["claude-fable-5"] == 50


@pytest.mark.unit
def test_compute_excludes_out_of_window(tmp_path):
    now_ms = int(time.time() * 1000)
    old = now_ms - 6 * 3600 * 1000  # 6h ago — row is outside the 5h window
    u = {"input_tokens": 100, "output_tokens": 100}
    root = _proj(tmp_path, "proj-a", [_assistant("claude-opus-4-8", u, ts_ms=old)])
    out = usage._compute(root, window_hours=5, now_ms=now_ms)
    assert out["window_tokens"] == 0
    assert out["by_model"] == {}


@pytest.mark.unit
def test_compute_skips_synthetic_and_missing_ts(tmp_path):
    now_ms = int(time.time() * 1000)
    inwin = now_ms - 60_000
    synthetic = {"type": "assistant", "timestamp":
                 datetime.fromtimestamp(inwin / 1000, tz=timezone.utc).isoformat().replace("+00:00", "Z"),
                 "message": {"role": "assistant", "model": "<synthetic>",
                             "usage": {"input_tokens": 5, "output_tokens": 5}}}
    no_ts = _assistant("claude-opus-4-8", {"input_tokens": 9, "output_tokens": 9})  # no timestamp
    root = _proj(tmp_path, "proj-a", [synthetic, no_ts])
    out = usage._compute(root, window_hours=5, now_ms=now_ms)
    assert out["window_tokens"] == 0


@pytest.mark.unit
def test_compute_missing_projects_dir(tmp_path):
    out = usage._compute(tmp_path / "nope", window_hours=5, now_ms=2_000_000_000_000)
    assert out == {"window_hours": 5, "window_tokens": 0, "by_model": {}}


# ---------- 5h budget / percent ----------

@pytest.mark.unit
def test_budget_from_env(monkeypatch):
    monkeypatch.setenv("CLAUDE_FLEET_5H_TOKEN_BUDGET", "1_000_000")
    assert usage._budget() == 1_000_000


@pytest.mark.unit
def test_budget_zero_disables_percent(monkeypatch):
    monkeypatch.setenv("CLAUDE_FLEET_5H_TOKEN_BUDGET", "0")
    assert usage._budget() is None


@pytest.mark.unit
def test_budget_default_when_unset(monkeypatch, tmp_path):
    monkeypatch.delenv("CLAUDE_FLEET_5H_TOKEN_BUDGET", raising=False)
    monkeypatch.setattr(usage, "_BUDGET_FILE", tmp_path / "nope")
    assert usage._budget() == usage.DEFAULT_5H_BUDGET


@pytest.mark.unit
def test_summary_attaches_percent(monkeypatch):
    monkeypatch.setenv("CLAUDE_FLEET_5H_TOKEN_BUDGET", "53000000")
    monkeypatch.setattr(usage, "_compute",
                        lambda *a, **k: {"window_hours": 5, "window_tokens": 5_300_000, "by_model": {}})
    usage._cache["data"] = None  # bypass cache
    out = usage.summary()
    assert out["window_budget"] == 53_000_000
    assert out["window_pct"] == 10.0
