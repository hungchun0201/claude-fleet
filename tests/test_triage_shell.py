"""Triage for sessions whose turn ended but a background shell lingers.

status == "shell" means the turn is over (end_turn) and the agent is idle —
only a background shell stays alive (a forgotten server, or a bare `&` that
never exits). It must NOT read "working": that masks a finished/abandoned
session. It reads "completed" (or "working" only if a tracked
run_in_background task is genuinely active), and the lingering shell is shown
as a 🐚 badge driven by w.status on the card.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.patrol import classify


def _end_turn(tmp_path: Path) -> str:
    p = tmp_path / "t.jsonl"
    p.write_text(json.dumps({
        "type": "assistant",
        "message": {"role": "assistant", "model": "claude-opus-4-8",
                    "stop_reason": "end_turn",
                    "content": [{"type": "text", "text": "全部完成。"}]},
    }) + "\n")
    return str(p)


@pytest.mark.unit
def test_shell_with_finished_turn_is_completed(tmp_path):
    w = {"status": "shell", "idle_seconds": 90, "name": "s",
         "transcript_path": _end_turn(tmp_path)}
    tri = classify(w)
    assert tri["triage"] == "completed"   # not "working"
    assert "完成" in tri["reason"]


@pytest.mark.unit
def test_shell_finished_long_idle_is_closeable(tmp_path):
    w = {"status": "shell", "idle_seconds": 4000, "name": "s",
         "transcript_path": _end_turn(tmp_path)}
    assert classify(w)["triage"] == "closeable"


@pytest.mark.unit
def test_shell_with_active_background_task_still_working(tmp_path):
    # A genuinely-running tracked background task wins over the finished turn.
    w = {"status": "shell", "idle_seconds": 90, "name": "s",
         "transcript_path": _end_turn(tmp_path),
         "background_tasks": [{"type": "bash_bg", "command": "npm run dev",
                               "description": "dev server"}]}
    tri = classify(w)
    assert tri["triage"] == "working"
    assert "dev server" in tri["reason"] or "npm run dev" in tri["reason"]
