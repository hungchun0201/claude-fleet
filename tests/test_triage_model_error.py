"""Triage for API-error transcripts (e.g. the selected model was disabled).

Regression: when a session's model is revoked mid-run, Claude Code writes a
synthetic assistant row flagged isApiErrorMessage with stop_reason that is
neither end_turn nor tool_use. classify() used to fall through to the
idle<5min catch-all and label such a stuck session "working". It must read
"stalled" with a /model hint instead.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.patrol import classify


def _write(tmp_path: Path, rows: list[dict]) -> str:
    p = tmp_path / "t.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    return str(p)


def _model_error_row(model: str = "claude-fable-5[1m]") -> dict:
    return {
        "type": "assistant",
        "isApiErrorMessage": True,
        "message": {
            "role": "assistant",
            "model": "<synthetic>",
            "stop_reason": "stop_sequence",
            "content": [{
                "type": "text",
                "text": (f"There's an issue with the selected model ({model}). "
                         "It may not exist or you may not have access to it. "
                         "Run /model to pick a different model."),
            }],
        },
    }


@pytest.mark.unit
def test_model_unavailable_is_stalled_not_working(tmp_path):
    # idle < 5min: the old catch-all would have said "working".
    tp = _write(tmp_path, [_model_error_row()])
    w = {"status": "idle", "idle_seconds": 80, "name": "s", "transcript_path": tp}
    tri = classify(w)
    assert tri["triage"] == "stalled"
    assert "claude-fable-5" in tri["reason"]
    assert "/model" in tri["reason"] or "/model" in tri["suggestion"]


@pytest.mark.unit
def test_model_unavailable_stalled_even_when_busy_flag_lingers(tmp_path):
    # A stale status=busy must not mask the error (idle>=5min reaches info path).
    tp = _write(tmp_path, [_model_error_row()])
    w = {"status": "idle", "idle_seconds": 600, "name": "s", "transcript_path": tp}
    assert classify(w)["triage"] == "stalled"


@pytest.mark.unit
def test_generic_api_error_is_stalled(tmp_path):
    row = {
        "type": "assistant",
        "isApiErrorMessage": True,
        "message": {
            "role": "assistant", "model": "<synthetic>", "stop_reason": "stop_sequence",
            "content": [{"type": "text", "text": "API Error: 529 overloaded_error"}],
        },
    }
    tp = _write(tmp_path, [row])
    w = {"status": "idle", "idle_seconds": 30, "name": "s", "transcript_path": tp}
    tri = classify(w)
    assert tri["triage"] == "stalled"
    assert "overloaded" in tri["reason"]


@pytest.mark.unit
def test_clean_end_turn_still_completed(tmp_path):
    # Guard: a normal finished turn is unaffected by the new branch.
    row = {
        "type": "assistant",
        "message": {"role": "assistant", "model": "claude-opus-4-8",
                    "stop_reason": "end_turn",
                    "content": [{"type": "text", "text": "done"}]},
    }
    tp = _write(tmp_path, [row])
    w = {"status": "idle", "idle_seconds": 600, "name": "s", "transcript_path": tp}
    assert classify(w)["triage"] == "completed"
