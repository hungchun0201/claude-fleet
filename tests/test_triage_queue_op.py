"""A queue-operation after end_turn must NOT read as 'working'.

queue-operation rows carry the user-input / task-notification queue (typeahead,
a /slash command, or delivery of a *completed* background task's notification).
An enqueue/dequeue after end_turn does not mean work is running — treating it as
such used to flip finished sessions to 'working'. Real background work comes from
extract_background_tasks (window_dict['background_tasks']).
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


_END_TURN = {
    "type": "assistant",
    "message": {"role": "assistant", "model": "claude-opus-4-8", "stop_reason": "end_turn",
                "content": [{"type": "text", "text": "Both done and pushed."}]},
}


@pytest.mark.unit
def test_dequeue_after_end_turn_is_completed(tmp_path):
    # /effort etc. leaves a 'dequeue' queue-op after the finished turn.
    rows = [_END_TURN,
            {"type": "queue-operation", "operation": "dequeue", "content": "<task-notification>…</task-notification>"},
            {"type": "user", "message": {"role": "user", "content": "x"}}]
    w = {"status": "idle", "idle_seconds": 800, "name": "s", "transcript_path": _write(tmp_path, rows)}
    assert classify(w)["triage"] == "completed"


@pytest.mark.unit
def test_enqueue_completed_notification_is_not_working(tmp_path):
    rows = [_END_TURN,
            {"type": "queue-operation", "operation": "enqueue",
             "content": "<task-notification><status>completed</status></task-notification>"}]
    w = {"status": "idle", "idle_seconds": 200, "name": "s", "transcript_path": _write(tmp_path, rows)}
    assert classify(w)["triage"] == "completed"


@pytest.mark.unit
def test_real_background_task_still_working(tmp_path):
    # The structural detector is the source of truth — it still wins.
    w = {"status": "idle", "idle_seconds": 200, "name": "s",
         "transcript_path": _write(tmp_path, [_END_TURN]),
         "background_tasks": [{"type": "bash_bg", "command": "npm run dev", "description": "dev server"}]}
    assert classify(w)["triage"] == "working"
