"""Tests for transcripts.extract_pending_wakeup (waiting-on-GPU detection)."""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from core.patrol import classify
from core.transcripts import _tail_lines, extract_pending_wakeup


def _iso(ms: int) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(ms / 1000)) + ".000Z"


def _assistant_row(content: list, ts_ms: int) -> dict:
    return {
        "type": "assistant",
        "timestamp": _iso(ts_ms),
        "message": {"role": "assistant", "content": content, "stop_reason": "tool_use"},
    }


def _wakeup_row(ts_ms: int, delay: int = 1800, reason: str = "", prompt: str = "") -> dict:
    return _assistant_row(
        [{
            "type": "tool_use",
            "id": "toolu_x",
            "name": "ScheduleWakeup",
            "input": {"delaySeconds": delay, "reason": reason, "prompt": prompt},
        }],
        ts_ms,
    )


def _tool_result_row(ts_ms: int) -> dict:
    return {
        "type": "user",
        "timestamp": _iso(ts_ms),
        "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "toolu_x", "content": "ok"},
        ]},
    }


def _user_text_row(ts_ms: int, text: str = "/loop check PACE jobs") -> dict:
    return {
        "type": "user",
        "timestamp": _iso(ts_ms),
        "message": {"role": "user", "content": [{"type": "text", "text": text}]},
    }


def _write(tmp_path: Path, rows: list[dict]) -> Path:
    p = tmp_path / "t.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    return p


NOW_MS = int(time.time() * 1000)
GPU_REASON = "Polling PACE for the three L40S jobs to start"


@pytest.mark.unit
def test_pending_gpu_wakeup_detected(tmp_path):
    # Arrange: transcript ends with ScheduleWakeup + its tool_result (sleeping)
    sched = NOW_MS - 60_000
    p = _write(tmp_path, [
        _user_text_row(sched - 10_000),
        _wakeup_row(sched, delay=1800, reason=GPU_REASON),
        _tool_result_row(sched + 1_000),
    ])

    # Act
    pw = extract_pending_wakeup(p)

    # Assert
    assert pw is not None
    assert pw["kind"] == "gpu"
    assert pw["reason"] == GPU_REASON
    assert pw["delay_seconds"] == 1800
    assert pw["wake_at_ms"] == pw["scheduled_at_ms"] + 1800 * 1000
    assert pw["overdue"] is False


@pytest.mark.unit
def test_generic_wakeup_not_tagged_gpu(tmp_path):
    sched = NOW_MS - 60_000
    p = _write(tmp_path, [
        _wakeup_row(sched, delay=600, reason="idle tick, nothing to watch"),
        _tool_result_row(sched + 1_000),
    ])
    pw = extract_pending_wakeup(p)
    assert pw is not None
    assert pw["kind"] == "generic"


@pytest.mark.unit
def test_fired_wakeup_is_not_pending(tmp_path):
    # The harness injected the /loop prompt after the wakeup fired.
    sched = NOW_MS - 3_600_000
    p = _write(tmp_path, [
        _wakeup_row(sched, reason=GPU_REASON),
        _tool_result_row(sched + 1_000),
        _user_text_row(sched + 1_800_000),
    ])
    assert extract_pending_wakeup(p) is None


@pytest.mark.unit
def test_later_assistant_action_clears_wakeup(tmp_path):
    sched = NOW_MS - 3_600_000
    p = _write(tmp_path, [
        _wakeup_row(sched, reason=GPU_REASON),
        _tool_result_row(sched + 1_000),
        _user_text_row(sched + 1_800_000),
        _assistant_row([{"type": "tool_use", "id": "toolu_y", "name": "Bash",
                         "input": {"command": "squeue"}}], sched + 1_810_000),
    ])
    assert extract_pending_wakeup(p) is None


@pytest.mark.unit
def test_thinking_only_row_after_wakeup_still_pending(tmp_path):
    sched = NOW_MS - 60_000
    p = _write(tmp_path, [
        _wakeup_row(sched, reason=GPU_REASON),
        _tool_result_row(sched + 1_000),
        _assistant_row([{"type": "thinking", "thinking": "..."}], sched + 2_000),
    ])
    pw = extract_pending_wakeup(p)
    assert pw is not None and pw["kind"] == "gpu"


@pytest.mark.unit
def test_no_wakeup_returns_none(tmp_path):
    p = _write(tmp_path, [
        _user_text_row(NOW_MS - 5_000),
        _assistant_row([{"type": "text", "text": "done."}], NOW_MS - 4_000),
    ])
    assert extract_pending_wakeup(p) is None


@pytest.mark.unit
def test_overdue_wakeup_flagged(tmp_path):
    # Scheduled 2h ago with a 10-min delay and never fired.
    sched = NOW_MS - 7_200_000
    p = _write(tmp_path, [
        _wakeup_row(sched, delay=600, reason=GPU_REASON),
        _tool_result_row(sched + 1_000),
    ])
    pw = extract_pending_wakeup(p)
    assert pw is not None
    assert pw["overdue"] is True


@pytest.mark.unit
def test_delay_clamped_to_runtime_range(tmp_path):
    sched = NOW_MS - 1_000
    p = _write(tmp_path, [
        _wakeup_row(sched, delay=99999, reason=GPU_REASON),
        _tool_result_row(sched + 500),
    ])
    pw = extract_pending_wakeup(p)
    assert pw is not None
    assert pw["delay_seconds"] == 3600


@pytest.mark.unit
def test_missing_file_returns_none(tmp_path):
    assert extract_pending_wakeup(tmp_path / "nope.jsonl") is None


@pytest.mark.unit
def test_malformed_input_returns_none(tmp_path):
    sched = NOW_MS - 1_000
    row = _wakeup_row(sched)
    row["message"]["content"][0]["input"] = {"delaySeconds": "not-a-number"}
    p = _write(tmp_path, [row, _tool_result_row(sched + 500)])
    assert extract_pending_wakeup(p) is None


@pytest.mark.unit
def test_errored_wakeup_call_is_not_pending(tmp_path):
    # The ScheduleWakeup call itself failed (e.g. rejected) — not sleeping.
    sched = NOW_MS - 60_000
    err = _tool_result_row(sched + 1_000)
    err["message"]["content"][0]["is_error"] = True
    p = _write(tmp_path, [_wakeup_row(sched, reason=GPU_REASON), err])
    assert extract_pending_wakeup(p) is None


# ---------- patrol.classify integration ----------

def _window(pw: dict | None, status: str = "busy") -> dict:
    return {
        "status": status,
        "idle_seconds": 600,
        "name": "gpu-session",
        "transcript_path": None,
        "pending_wakeup": pw,
    }


@pytest.mark.unit
def test_classify_pending_gpu_wakeup_is_working(tmp_path):
    pw = {"kind": "gpu", "reason": GPU_REASON, "wake_at_ms": NOW_MS + 600_000, "overdue": False}
    tri = classify(_window(pw))
    assert tri["triage"] == "working"
    assert "等 GPU" in tri["reason"]
    assert "下次唤醒" in tri["reason"]


@pytest.mark.unit
def test_classify_overdue_wakeup_is_stalled(tmp_path):
    pw = {"kind": "gpu", "reason": GPU_REASON, "wake_at_ms": NOW_MS - 600_000, "overdue": True}
    tri = classify(_window(pw, status="idle"))
    assert tri["triage"] == "stalled"
    assert "唤醒已过期" in tri["reason"]


@pytest.mark.unit
def test_classify_generic_wakeup_label(tmp_path):
    pw = {"kind": "generic", "reason": "idle tick", "wake_at_ms": NOW_MS + 600_000, "overdue": False}
    tri = classify(_window(pw))
    assert tri["triage"] == "working"
    assert "等待定时唤醒" in tri["reason"]


@pytest.mark.unit
def test_classify_permission_prompt_beats_wakeup(tmp_path):
    pw = {"kind": "gpu", "reason": GPU_REASON, "wake_at_ms": NOW_MS + 600_000, "overdue": False}
    w = _window(pw, status="waiting")
    w["waiting_for"] = "Bash"
    tri = classify(w)
    assert tri["triage"] == "waiting_perm"


# ---------- _tail_lines (seek-from-end tail) ----------

@pytest.mark.unit
def test_tail_lines_matches_full_read(tmp_path):
    rows = [{"type": "user", "i": i} for i in range(500)]
    p = _write(tmp_path, rows)
    tail = _tail_lines(p, 80)
    assert tail == rows[-80:]


@pytest.mark.unit
def test_tail_lines_file_shorter_than_n(tmp_path):
    rows = [{"type": "user", "i": i} for i in range(5)]
    p = _write(tmp_path, rows)
    assert _tail_lines(p, 80) == rows


@pytest.mark.unit
def test_tail_lines_skips_malformed_and_blank(tmp_path):
    p = tmp_path / "t.jsonl"
    p.write_text('{"a": 1}\nnot json\n\n{"b": 2}')  # no trailing newline
    assert _tail_lines(p, 10) == [{"a": 1}, {"b": 2}]


@pytest.mark.unit
def test_tail_lines_missing_file(tmp_path):
    assert _tail_lines(tmp_path / "nope.jsonl", 10) == []


@pytest.mark.unit
def test_tail_lines_long_lines_spanning_blocks(tmp_path):
    # Lines bigger than the initial 64 KB read block must still parse.
    rows = [{"type": "user", "pad": "x" * 100_000, "i": i} for i in range(4)]
    p = _write(tmp_path, rows)
    tail = _tail_lines(p, 2)
    assert [r["i"] for r in tail] == [2, 3]
