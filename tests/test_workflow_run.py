"""Tests for Workflow run detection (extract_background_tasks + active_workflow_run).

Workflows run in the background: the tool_use gets an immediate spawn ack
("Workflow launched in background. Task ID: ...") and completion arrives later
as a <task-notification>. The main turn usually ends right after launching,
so without detection the session triages as "completed" while agents still run.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from core.patrol import classify
from core.transcripts import active_workflow_run, extract_background_tasks

WF_SCRIPT = (
    "export const meta = {\n"
    "  name: 'review-crosscheck',\n"
    "  description: 'Cross-check each review point against the paper',\n"
    "  phases: [{ title: 'Check', detail: 'one reader per point' }],\n"
    "}\n"
    "const FINDINGS = { type: 'object', properties: { name: { type: 'string' } } }\n"
    "phase('Check')\n"
)


def _wf_use(tool_use_id: str, inp: dict | None = None, ts: str = "2026-06-10T06:02:05.000Z") -> dict:
    return {
        "type": "assistant", "timestamp": ts,
        "message": {"role": "assistant", "content": [{
            "type": "tool_use", "id": tool_use_id, "name": "Workflow",
            "input": inp if inp is not None else {"script": WF_SCRIPT},
        }]},
    }


def _wf_ack(tool_use_id: str, task_id: str = "wtask1", wf_dir: str = "/tmp/wf/wf_abc123-456") -> dict:
    txt = (
        f"Workflow launched in background. Task ID: {task_id}\n"
        "Summary: Cross-check each review point against the paper\n"
        f"Transcript dir: {wf_dir}\n"
        f"Script file: /tmp/scripts/review-crosscheck-wf_abc123-456.js\n"
        "(Edit this file with Write/Edit and re-invoke Workflow with {scriptPath: ...})\n"
        "Run ID: wf_abc123-456\n"
        "To resume after editing the script: Workflow({scriptPath: ..., resumeFromRunId: \"wf_abc123-456\"})\n\n"
        "You will be notified when it completes. Use /workflows to watch live progress."
    )
    return {
        "type": "user",
        "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": tool_use_id, "content": txt},
        ]},
    }


def _notif(task_id: str, tool_use_id: str, status: str = "completed") -> dict:
    return {
        "type": "user",
        "message": {"role": "user", "content": [{
            "type": "text",
            "text": (f"<task-notification>\n<task-id>{task_id}</task-id>\n"
                     f"<tool-use-id>{tool_use_id}</tool-use-id>\n<status>{status}</status>\n"
                     f"<summary>Dynamic workflow \"x\" {status}</summary>\n</task-notification>"),
        }]},
    }


def _write(tmp_path: Path, rows: list[dict]) -> Path:
    p = tmp_path / "t.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    return p


def _make_wf_dir(tmp_path: Path, started: int = 3, done: int = 2, age_s: int = 0) -> Path:
    d = tmp_path / "wfdir"
    d.mkdir()
    rows = [{"type": "started", "key": f"v2:{i}", "agentId": f"a{i}"} for i in range(started)]
    rows += [{"type": "result", "key": f"v2:{i}", "agentId": f"a{i}", "result": "ok"} for i in range(done)]
    (d / "journal.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    (d / "agent-a0.jsonl").write_text("{}\n")
    if age_s:
        old = time.time() - age_s
        for f in d.iterdir():
            os.utime(f, (old, old))
    return d


@pytest.mark.unit
def test_active_workflow_detected(tmp_path):
    p = _write(tmp_path, [_wf_use("toolu_w"), _wf_ack("toolu_w")])
    tasks = extract_background_tasks(p)
    assert len(tasks) == 1
    t = tasks[0]
    assert t["type"] == "workflow"
    assert t["task_id"] == "wtask1"
    assert t["workflow_name"] == "review-crosscheck"
    assert t["description"] == "Cross-check each review point against the paper"
    assert t["workflow_dir"] == "/tmp/wf/wf_abc123-456"
    assert t["run_id"] == "wf_abc123-456"
    assert t["is_gpu"] is False


@pytest.mark.unit
def test_workflow_name_fallbacks(tmp_path):
    # Predefined workflow: explicit name input.
    p = _write(tmp_path, [_wf_use("toolu_a", {"name": "find-flaky-tests"}), _wf_ack("toolu_a")])
    assert extract_background_tasks(p)[0]["workflow_name"] == "find-flaky-tests"
    # scriptPath re-invocation: stem minus the -wf_<runid> suffix.
    p = _write(tmp_path, [
        _wf_use("toolu_b", {"scriptPath": "/x/scripts/paper-figures-fanout-wf_7d6e41f1-420.js"}),
        _wf_ack("toolu_b"),
    ])
    assert extract_background_tasks(p)[0]["workflow_name"] == "paper-figures-fanout"


@pytest.mark.unit
def test_completion_notification_resolves(tmp_path):
    p = _write(tmp_path, [_wf_use("toolu_w"), _wf_ack("toolu_w"), _notif("wtask1", "toolu_w")])
    assert extract_background_tasks(p) == []


@pytest.mark.unit
def test_failed_notification_resolves(tmp_path):
    p = _write(tmp_path, [_wf_use("toolu_w"), _wf_ack("toolu_w"), _notif("wtask1", "toolu_w", "failed")])
    assert extract_background_tasks(p) == []


@pytest.mark.unit
def test_taskstop_resolves(tmp_path):
    stop = {
        "type": "assistant",
        "message": {"role": "assistant", "content": [{
            "type": "tool_use", "id": "toolu_s", "name": "TaskStop",
            "input": {"task_id": "wtask1"},
        }]},
    }
    p = _write(tmp_path, [_wf_use("toolu_w"), _wf_ack("toolu_w"), stop])
    assert extract_background_tasks(p) == []


@pytest.mark.unit
def test_script_error_resolves(tmp_path):
    err = {
        "type": "user",
        "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "toolu_w",
             "content": "Workflow script error: unexpected token", "is_error": True},
        ]},
    }
    p = _write(tmp_path, [_wf_use("toolu_w"), err])
    assert extract_background_tasks(p) == []


@pytest.mark.unit
def test_workflow_not_a_gpu_waiter(tmp_path):
    # Even a workflow whose summary mentions GPUs is active work, not a wait.
    from core.transcripts import gpu_wait_from_background
    p = _write(tmp_path, [_wf_use("toolu_w"), _wf_ack("toolu_w")])
    assert gpu_wait_from_background(extract_background_tasks(p)) is None


@pytest.mark.unit
def test_active_workflow_run_progress(tmp_path):
    wf_dir = _make_wf_dir(tmp_path, started=3, done=2)
    ts = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(time.time() - 120))
    p = _write(tmp_path, [_wf_use("toolu_w", ts=ts), _wf_ack("toolu_w", wf_dir=str(wf_dir))])
    run = active_workflow_run(extract_background_tasks(p))
    assert run is not None
    assert run["name"] == "review-crosscheck"
    assert run["agents_started"] == 3
    assert run["agents_done"] == 2
    assert run["elapsed_s"] == pytest.approx(120, abs=5)
    assert run["silent_s"] is not None and run["silent_s"] < 60
    assert run["stalled"] is False


@pytest.mark.unit
def test_active_workflow_run_stalled(tmp_path):
    wf_dir = _make_wf_dir(tmp_path, started=2, done=1, age_s=3600)
    p = _write(tmp_path, [_wf_use("toolu_w"), _wf_ack("toolu_w", wf_dir=str(wf_dir))])
    run = active_workflow_run(extract_background_tasks(p))
    assert run["stalled"] is True
    assert run["silent_s"] >= 3500


@pytest.mark.unit
def test_active_workflow_run_missing_dir(tmp_path):
    # Run dir gone (or never created): still report the run, just without progress.
    p = _write(tmp_path, [_wf_use("toolu_w"), _wf_ack("toolu_w", wf_dir=str(tmp_path / "nope"))])
    run = active_workflow_run(extract_background_tasks(p))
    assert run is not None
    assert run["agents_started"] is None
    assert run["stalled"] is False


@pytest.mark.unit
def test_active_workflow_run_none():
    assert active_workflow_run([]) is None
    assert active_workflow_run([{"type": "bash_bg", "is_gpu": True}]) is None


def _end_turn_transcript(tmp_path: Path) -> str:
    p = tmp_path / "et.jsonl"
    p.write_text(json.dumps({
        "type": "assistant",
        "message": {"role": "assistant", "stop_reason": "end_turn",
                    "content": [{"type": "text", "text": "已推上（46d76b9）。"}]},
    }) + "\n")
    return str(p)


@pytest.mark.unit
def test_classify_live_workflow_beats_completed(tmp_path):
    # THE regression: turn ended (end_turn) + idle 5m, but a workflow is
    # still running → must read "working", not "completed".
    tp = _end_turn_transcript(tmp_path)
    w = {"status": "idle", "idle_seconds": 300, "name": "s", "transcript_path": tp}
    assert classify(w)["triage"] == "completed"  # sanity: without workflow

    w["workflow_run"] = {"name": "review-crosscheck", "elapsed_s": 300,
                         "agents_started": 6, "agents_done": 4, "stalled": False}
    tri = classify(w)
    assert tri["triage"] == "working"
    assert "review-crosscheck" in tri["reason"]
    assert "4/6" in tri["reason"]


@pytest.mark.unit
def test_classify_stalled_workflow(tmp_path):
    w = {
        "status": "idle", "idle_seconds": 2000, "name": "s",
        "transcript_path": _end_turn_transcript(tmp_path),
        "workflow_run": {"name": "fanout", "elapsed_s": 2400, "silent_s": 1200,
                         "agents_started": 8, "agents_done": 3, "stalled": True},
    }
    tri = classify(w)
    assert tri["triage"] == "stalled"
    assert "fanout" in tri["reason"]
