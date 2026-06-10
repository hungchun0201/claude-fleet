"""Tests for extract_background_tasks / gpu_wait_from_background.

Covers the modern background-task lifecycle: spawn-ack tool_result,
<task-notification> completion (as user text, queue-operation, attachment),
TaskStop, and out-of-order transcript rows.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.patrol import classify
from core.transcripts import extract_background_tasks, gpu_wait_from_background

GPU_CMD = "until ssh pace 'sacct -j 9776397 -X -n -o State | grep -q COMPLETED'; do sleep 180; done"
GPU_DESC = "Wait for a100 clocktel jobs"


def _bg_bash_use(tool_use_id: str, cmd: str = GPU_CMD, desc: str = GPU_DESC, ts: str = "2026-06-10T06:02:05.000Z") -> dict:
    return {
        "type": "assistant", "timestamp": ts,
        "message": {"role": "assistant", "content": [{
            "type": "tool_use", "id": tool_use_id, "name": "Bash",
            "input": {"command": cmd, "description": desc, "run_in_background": True},
        }]},
    }


def _monitor_use(tool_use_id: str, ts: str = "2026-06-10T06:00:00.000Z") -> dict:
    return {
        "type": "assistant", "timestamp": ts,
        "message": {"role": "assistant", "content": [{
            "type": "tool_use", "id": tool_use_id, "name": "Monitor",
            "input": {"command": "ssh pace squeue", "description": "PACE jobs monitor", "persistent": True},
        }]},
    }


def _ack(tool_use_id: str, task_id: str, monitor: bool = False) -> dict:
    txt = (f"Monitor started (task {task_id}, persistent — runs until TaskStop or session end). "
           "You will be notified on each event."
           if monitor else
           f"Command running in background with ID: {task_id}. Output is being written to: /tmp/x. "
           "You will be notified when it completes.")
    return {
        "type": "user",
        "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": tool_use_id, "content": txt},
        ]},
    }


def _notif_text(task_id: str, tool_use_id: str, status: str = "completed") -> str:
    return (f"<task-notification>\n<task-id>{task_id}</task-id>\n"
            f"<tool-use-id>{tool_use_id}</tool-use-id>\n<status>{status}</status>\n"
            f"<summary>Background command finished</summary>\n</task-notification>")


def _user_notif(task_id: str, tool_use_id: str) -> dict:
    return {
        "type": "user",
        "message": {"role": "user", "content": [
            {"type": "text", "text": _notif_text(task_id, tool_use_id)},
        ]},
    }


def _monitor_event_notif(task_id: str) -> dict:
    return {
        "type": "user",
        "message": {"role": "user", "content": [{
            "type": "text",
            "text": (f"<task-notification>\n<task-id>{task_id}</task-id>\n"
                     "<summary>Monitor event: \"PACE jobs state transitions\"</summary>\n"
                     "</task-notification>"),
        }]},
    }


def _write(tmp_path: Path, rows: list[dict]) -> Path:
    p = tmp_path / "t.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    return p


@pytest.mark.unit
def test_active_bg_waiter_detected(tmp_path):
    p = _write(tmp_path, [_bg_bash_use("toolu_a"), _ack("toolu_a", "btask1")])
    tasks = extract_background_tasks(p)
    assert len(tasks) == 1
    t = tasks[0]
    assert t["task_id"] == "btask1"
    assert t["is_gpu"] is True
    assert t["poll_interval_s"] == 180


@pytest.mark.unit
def test_spawn_ack_does_not_resolve_task(tmp_path):
    # Regression: the old code treated ANY tool_result as completion,
    # so modern bg tasks (which ack immediately) never showed as active.
    p = _write(tmp_path, [_bg_bash_use("toolu_a"), _ack("toolu_a", "btask1")])
    assert len(extract_background_tasks(p)) == 1


@pytest.mark.unit
def test_completion_notification_resolves(tmp_path):
    p = _write(tmp_path, [
        _bg_bash_use("toolu_a"), _ack("toolu_a", "btask1"),
        _user_notif("btask1", "toolu_a"),
    ])
    assert extract_background_tasks(p) == []


@pytest.mark.unit
def test_queue_operation_notification_resolves(tmp_path):
    p = _write(tmp_path, [
        _bg_bash_use("toolu_a"), _ack("toolu_a", "btask1"),
        {"type": "queue-operation", "operation": "enqueue",
         "content": _notif_text("btask1", "toolu_a", status="failed")},
    ])
    assert extract_background_tasks(p) == []


@pytest.mark.unit
def test_attachment_notification_resolves(tmp_path):
    p = _write(tmp_path, [
        _bg_bash_use("toolu_a"), _ack("toolu_a", "btask1"),
        {"type": "attachment",
         "attachment": {"type": "queued_command", "commandMode": "task-notification",
                        "prompt": _notif_text("btask1", "toolu_a")}},
    ])
    assert extract_background_tasks(p) == []


@pytest.mark.unit
def test_out_of_order_ack_before_use(tmp_path):
    # Regression: transcripts can write the tool_result row before the
    # tool_use row; the ack must still be matched.
    p = _write(tmp_path, [_ack("toolu_a", "btask1"), _bg_bash_use("toolu_a")])
    tasks = extract_background_tasks(p)
    assert len(tasks) == 1
    assert tasks[0]["task_id"] == "btask1"


@pytest.mark.unit
def test_monitor_event_does_not_resolve_persistent_monitor(tmp_path):
    p = _write(tmp_path, [
        _monitor_use("toolu_m"), _ack("toolu_m", "bmon1", monitor=True),
        _monitor_event_notif("bmon1"),
        _monitor_event_notif("bmon1"),
    ])
    tasks = extract_background_tasks(p)
    assert len(tasks) == 1
    assert tasks[0]["type"] == "monitor"


@pytest.mark.unit
def test_monitor_completion_resolves_even_with_event_like_summary(tmp_path):
    # The assistant-authored description is embedded verbatim in the summary;
    # "Monitor event" appearing there must not block a terminal notification.
    notif = {
        "type": "user",
        "message": {"role": "user", "content": [{
            "type": "text",
            "text": ("<task-notification>\n<task-id>bmon1</task-id>\n"
                     "<tool-use-id>toolu_m</tool-use-id>\n<status>completed</status>\n"
                     "<summary>Monitor \"Monitor event watcher\" stream ended</summary>\n"
                     "</task-notification>"),
        }]},
    }
    p = _write(tmp_path, [_monitor_use("toolu_m"), _ack("toolu_m", "bmon1", monitor=True), notif])
    assert extract_background_tasks(p) == []


@pytest.mark.unit
def test_taskstop_resolves_monitor(tmp_path):
    stop = {
        "type": "assistant",
        "message": {"role": "assistant", "content": [{
            "type": "tool_use", "id": "toolu_s", "name": "TaskStop",
            "input": {"task_id": "bmon1"},
        }]},
    }
    p = _write(tmp_path, [_monitor_use("toolu_m"), _ack("toolu_m", "bmon1", monitor=True), stop])
    assert extract_background_tasks(p) == []


@pytest.mark.unit
def test_spawn_error_resolves(tmp_path):
    err = {
        "type": "user",
        "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "toolu_a",
             "content": "spawn failed", "is_error": True},
        ]},
    }
    p = _write(tmp_path, [_bg_bash_use("toolu_a"), err])
    assert extract_background_tasks(p) == []


@pytest.mark.unit
def test_gpu_wait_picks_most_recent_gpu_task():
    tasks = [
        {"is_gpu": True, "description": "old waiter", "command": "x", "poll_interval_s": 60},
        {"is_gpu": False, "description": "npm build", "command": "npm run build", "poll_interval_s": None},
        {"is_gpu": True, "description": "new waiter", "command": "y", "poll_interval_s": 180},
    ]
    pw = gpu_wait_from_background(tasks)
    assert pw is not None
    assert pw["reason"] == "new waiter"
    assert pw["poll_interval_s"] == 180
    assert pw["wake_at_ms"] is None
    assert pw["source"] == "background"


@pytest.mark.unit
def test_gpu_wait_none_without_gpu_tasks():
    assert gpu_wait_from_background([]) is None
    assert gpu_wait_from_background([{"is_gpu": False, "description": "d", "command": "c"}]) is None


@pytest.mark.unit
def test_classify_background_gpu_waiter_is_working():
    w = {
        "status": "shell", "idle_seconds": 1200, "name": "s", "transcript_path": None,
        "pending_wakeup": {"kind": "gpu", "source": "background", "reason": GPU_DESC,
                           "wake_at_ms": None, "poll_interval_s": 180, "overdue": False},
    }
    tri = classify(w)
    assert tri["triage"] == "working"
    assert "等 GPU" in tri["reason"]
    assert "每 ~3m 检查" in tri["reason"]
