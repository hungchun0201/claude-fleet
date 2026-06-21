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

GPU_CMD = "until ssh -o ConnectTimeout=30 pace 'sacct -j 9776397,9777747 -X -n -o State | grep -q COMPLETED'; do sleep 180; done"
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
           f"Command running in background with ID: {task_id}. Output is being written to: "
           f"/tmp/tasks/{task_id}.output. You will be notified when it completes.")
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
    # Queue-poll metadata parsed from the waiter command + spawn ack.
    assert t["ssh_host"] == "pace"
    assert t["job_ids"] == ["9776397", "9777747"]
    assert t["output_file"] == "/tmp/tasks/btask1.output"


@pytest.mark.unit
def test_gpu_wait_carries_poll_metadata(tmp_path):
    p = _write(tmp_path, [_bg_bash_use("toolu_a"), _ack("toolu_a", "btask1")])
    pw = gpu_wait_from_background(extract_background_tasks(p))
    assert pw["ssh_host"] == "pace"
    assert pw["job_ids"] == ["9776397", "9777747"]
    assert pw["output_file"] == "/tmp/tasks/btask1.output"


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
def test_codex_review_bg_task_not_tagged_gpu(tmp_path):
    # A codex exec reviewing GPU-related content is not a GPU waiter.
    p = _write(tmp_path, [
        _bg_bash_use("toolu_a",
                     cmd="codex exec \"review the H100 GPU memory article\" | tee out.md",
                     desc="Run Codex review round 1 via CLI in background"),
        _ack("toolu_a", "btask1"),
    ])
    tasks = extract_background_tasks(p)
    assert len(tasks) == 1
    assert tasks[0]["is_gpu"] is False
    assert gpu_wait_from_background(tasks) is None


@pytest.mark.unit
def test_gpu_wait_none_without_gpu_tasks():
    assert gpu_wait_from_background([]) is None
    assert gpu_wait_from_background([{"is_gpu": False, "description": "d", "command": "c"}]) is None


@pytest.mark.unit
def test_plain_remote_command_on_pace_not_tagged_gpu(tmp_path):
    # Regression: a du scan over `ssh pace` is a remote chore, not a GPU wait.
    # The bare hostname must not classify; only Slurm/GPU tokens do.
    p = _write(tmp_path, [
        _bg_bash_use("toolu_a",
                     cmd="ssh pace 'S=/storage/scratch1/1/hlin464; du -sh $S/* 2>/dev/null | sort -rh | head -25'",
                     desc="Disk usage breakdown of scratch directory"),
        _ack("toolu_a", "btask1"),
    ])
    tasks = extract_background_tasks(p)
    assert len(tasks) == 1
    assert tasks[0]["is_gpu"] is False
    assert gpu_wait_from_background(tasks) is None


def _end_turn_transcript(tmp_path: Path, text: str) -> str:
    p = tmp_path / "et.jsonl"
    p.write_text(json.dumps({
        "type": "assistant",
        "message": {"role": "assistant", "stop_reason": "end_turn",
                    "content": [{"type": "text", "text": text}]},
    }) + "\n")
    return str(p)


@pytest.mark.unit
def test_classify_active_bg_tasks_beat_completed(tmp_path):
    # Turn ended + idle, but du shells still run in background → working.
    tp = _end_turn_transcript(tmp_path, "兩個 du 還在跑，完成通知一到我就彙整。")
    w = {
        "status": "idle", "idle_seconds": 600, "name": "s", "transcript_path": tp,
        "background_tasks": [
            {"type": "bash_bg", "description": "Disk usage breakdown of scratch", "command": "ssh pace du", "is_gpu": False},
            {"type": "bash_bg", "description": "Disk usage breakdown of project", "command": "ssh pace du", "is_gpu": False},
        ],
    }
    tri = classify(w)
    assert tri["triage"] == "working"
    assert "2 background tasks running" in tri["reason"]
    assert "Disk usage breakdown of project" in tri["reason"]


@pytest.mark.unit
def test_classify_prose_mentioning_background_is_not_working(tmp_path):
    # Regression: an idle session whose last summary merely QUOTES the word
    # "background" (e.g. "Workflow launched in background. Task ID: ...")
    # used to flip to working via a keyword match. With no actual background
    # tasks it must read completed.
    tp = _end_turn_transcript(
        tmp_path,
        "完成了。spawn ack（Workflow launched in background. Task ID: …）解析出 task id，等待通知即可。",
    )
    w = {"status": "idle", "idle_seconds": 600, "name": "s",
         "transcript_path": tp, "background_tasks": []}
    assert classify(w)["triage"] == "completed"


# --- Workflow recovery from an orphaned spawn-ack ---------------------------
# A long-running Workflow's tool_use can scroll out of a mirrored transcript
# tail (remote/lab cards only mirror the last N bytes) while the run is still
# live. The spawn-ack alone must keep the session reading "working", not
# "completed". Mirrors the live lab-kvoffloading-vllm bug.

def _workflow_use(tool_use_id: str, name: str = "vllm-kv-offload-research",
                  ts: str = "2026-06-20T22:00:00.000Z") -> dict:
    return {
        "type": "assistant", "timestamp": ts,
        "message": {"role": "assistant", "content": [{
            "type": "tool_use", "id": tool_use_id, "name": "Workflow",
            "input": {"name": name},
        }]},
    }


def _workflow_ack(tool_use_id: str, task_id: str = "wpaoxypnr",
                  run_id: str = "wf_f6a8c4c7-e0b",
                  name: str = "vllm-kv-offload-research",
                  ts: str = "2026-06-20T22:00:01.000Z") -> dict:
    # Real Workflow ack shape: only "launched in background" + "will be notified"
    # — no "running in background" — plus Task ID / Transcript dir / Script file.
    txt = (f"Workflow launched in background. Task ID: {task_id}\n"
           f"Summary: Exhaustive research report on vLLM native KV offloading\n"
           f"Transcript dir: /home/u/.claude/projects/p/subagents/workflows/{run_id}\n"
           f"Script file: /home/u/.claude/projects/p/workflows/scripts/{name}-{run_id}.js\n"
           f"Run ID: {run_id}\n\nYou will be notified when it completes.")
    return {
        "type": "user", "timestamp": ts,
        "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": tool_use_id, "content": txt},
        ]},
    }


@pytest.mark.unit
def test_orphaned_workflow_ack_recovered(tmp_path):
    # Only the ack survived the tail (no tool_use) — must still be an active run.
    p = _write(tmp_path, [_workflow_ack("toolu_gone")])
    tasks = extract_background_tasks(p)
    assert len(tasks) == 1
    t = tasks[0]
    assert t["type"] == "workflow"
    assert t["task_id"] == "wpaoxypnr"
    assert t["run_id"] == "wf_f6a8c4c7-e0b"
    assert t["workflow_name"] == "vllm-kv-offload-research"


@pytest.mark.unit
def test_orphaned_workflow_ack_resolved_by_notification(tmp_path):
    # If the ack is in the tail, its later completion notification is too — so a
    # finished workflow is never falsely recovered as active.
    notif = _user_notif("wpaoxypnr", "toolu_gone")
    p = _write(tmp_path, [_workflow_ack("toolu_gone"), notif])
    assert extract_background_tasks(p) == []


@pytest.mark.unit
def test_orphaned_workflow_ack_resolved_by_taskstop(tmp_path):
    stop = {
        "type": "assistant",
        "message": {"role": "assistant", "content": [{
            "type": "tool_use", "id": "toolu_s", "name": "TaskStop",
            "input": {"task_id": "wpaoxypnr"},
        }]},
    }
    p = _write(tmp_path, [_workflow_ack("toolu_gone"), stop])
    assert extract_background_tasks(p) == []


@pytest.mark.unit
def test_orphaned_bash_ack_not_recovered(tmp_path):
    # A plain bg-Bash ack with no tool_use (no run dir / run id) is left alone —
    # only Workflow acks are recovered.
    p = _write(tmp_path, [_ack("toolu_gone", "btask1")])
    assert extract_background_tasks(p) == []


@pytest.mark.unit
def test_normal_workflow_with_tool_use_not_double_counted(tmp_path):
    # tool_use present → handled normally; the orphan pass must not re-add it.
    p = _write(tmp_path, [_workflow_use("toolu_w"), _workflow_ack("toolu_w")])
    tasks = extract_background_tasks(p)
    assert len(tasks) == 1
    assert tasks[0]["workflow_name"] == "vllm-kv-offload-research"


@pytest.mark.unit
def test_classify_orphaned_workflow_is_working(tmp_path):
    # The end-to-end regression: ack-only transcript + ended turn → working.
    p = _write(tmp_path, [_workflow_ack("toolu_gone")])
    from core.transcripts import active_workflow_run
    tasks = extract_background_tasks(p)
    w = {"status": "busy", "idle_seconds": 1000, "name": "s",
         "transcript_path": str(p), "background_tasks": tasks,
         "workflow_run": active_workflow_run(tasks, read_progress=False)}
    tri = classify(w)
    assert tri["triage"] == "working"
    assert "vllm-kv-offload-research" in tri["reason"]


@pytest.mark.unit
def test_classify_background_gpu_waiter_is_working():
    w = {
        "status": "shell", "idle_seconds": 1200, "name": "s", "transcript_path": None,
        "pending_wakeup": {"kind": "gpu", "source": "background", "reason": GPU_DESC,
                           "wake_at_ms": None, "poll_interval_s": 180, "overdue": False},
    }
    tri = classify(w)
    assert tri["triage"] == "working"
    assert "Waiting on GPU" in tri["reason"]
    assert "checks every ~3m" in tri["reason"]
