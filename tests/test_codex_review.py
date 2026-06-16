"""Tests for live Codex review detection (codex.py + transcripts.codex_call_marker)."""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from core.codex import _cwd_related, _parse_etime, detect_codex_review, rollout_current_action
from core.patrol import classify
from core.transcripts import codex_call_marker

NOW_MS = int(time.time() * 1000)


# ---------- _parse_etime ----------

@pytest.mark.unit
def test_parse_etime_formats():
    assert _parse_etime("55:11") == 55 * 60 + 11
    assert _parse_etime("23:45:00") == 23 * 3600 + 45 * 60
    assert _parse_etime("02-00:55:11") == 2 * 86400 + 55 * 60 + 11
    assert _parse_etime("garbage") == 0


# ---------- _cwd_related ----------

@pytest.mark.unit
def test_cwd_related():
    assert _cwd_related("/a/b", "/a/b")
    assert _cwd_related("/a/b/c", "/a/b")
    assert _cwd_related("/a/b", "/a/b/c")
    assert not _cwd_related("/a/bb", "/a/b")
    assert not _cwd_related("", "/a/b")


# ---------- detect_codex_review ----------

def _window(pid=100, cwd="/proj", status="busy"):
    return {"pid": pid, "cwd": cwd, "status": status}


def _rollout(started_offset_s: int, cwd="/proj", originator="codex_exec",
             sid="sid-1", last_write_offset_s: int | None = None):
    lw = last_write_offset_s if last_write_offset_s is not None else started_offset_s
    return {
        "path": "/dev/null", "codex_session_id": sid, "cwd": cwd,
        "originator": originator,
        "started_at_ms": NOW_MS - started_offset_s * 1000,
        "last_write_ms": NOW_MS - lw * 1000,
        "size": 1000,
    }


@pytest.mark.unit
def test_exec_review_detected_and_stalled():
    exec_map = {100: {"elapsed_s": 7200, "pid": 200, "command": "node codex exec ..."}}
    # Rollout started just after the process, silent for 1h -> stalled.
    rolls = [_rollout(7100, last_write_offset_s=3600)]
    cr = detect_codex_review(_window(), exec_map, rolls, None)
    assert cr is not None
    assert cr["source"] == "exec"
    assert cr["stalled"] is True
    assert cr["codex_session_id"] == "sid-1"


@pytest.mark.unit
def test_exec_picks_rollout_closest_to_process_start():
    exec_map = {100: {"elapsed_s": 7200, "pid": 200, "command": "codex exec"}}
    rolls = [
        _rollout(600, sid="newest-round"),   # a later round, fresh
        _rollout(7100, sid="own-rollout", last_write_offset_s=7000),
    ]
    cr = detect_codex_review(_window(), exec_map, rolls, None)
    assert cr["codex_session_id"] == "own-rollout"


@pytest.mark.unit
def test_exec_ignores_prior_rounds_rollout():
    # A previous review round finished 5 min before this exec started; it
    # must NOT be attributed to the new process (would show a false stalled
    # flag and the wrong session id).
    exec_map = {100: {"elapsed_s": 30, "pid": 200, "command": "codex exec"}}
    rolls = [
        _rollout(330, sid="prior-round", last_write_offset_s=290),
        _rollout(25, sid="current-round", last_write_offset_s=2),
    ]
    cr = detect_codex_review(_window(), exec_map, rolls, None)
    assert cr["codex_session_id"] == "current-round"
    assert cr["stalled"] is False


@pytest.mark.unit
def test_exec_streaming_not_stalled():
    exec_map = {100: {"elapsed_s": 600, "pid": 200, "command": "codex exec"}}
    rolls = [_rollout(580, last_write_offset_s=10)]
    cr = detect_codex_review(_window(), exec_map, rolls, None)
    assert cr["stalled"] is False
    assert cr["streaming"] is True


@pytest.mark.unit
def test_mcp_review_matched_by_cwd_and_time():
    marker = NOW_MS - 5400 * 1000
    # Rollout still streaming (wrote 10s ago) -> healthy.
    rolls = [_rollout(5390, originator="codex_cli_rs", sid="mcp-sid",
                      last_write_offset_s=10)]
    cr = detect_codex_review(_window(), {}, rolls, marker)
    assert cr is not None
    assert cr["source"] == "mcp"
    assert cr["codex_session_id"] == "mcp-sid"
    assert cr["streaming"] is True
    assert cr["stalled"] is False
    assert 5380 <= cr["elapsed_s"] <= 5400


@pytest.mark.unit
def test_mcp_rollout_silence_flags_stalled():
    # 2026-06-10 Stall B/C signature: MCP rollouts DO stream in codex 0.137,
    # so a rollout frozen for 20 min during an in-flight call is a hang.
    marker = NOW_MS - 5400 * 1000
    rolls = [_rollout(5390, originator="codex_cli_rs", sid="mcp-sid",
                      last_write_offset_s=20 * 60)]
    cr = detect_codex_review(_window(), {}, rolls, marker)
    assert cr["source"] == "mcp"
    assert cr["stalled"] is True
    assert cr["stall_reason"] == "silent"


@pytest.mark.unit
def test_mcp_marker_without_rollout_still_reported():
    marker = NOW_MS - 300 * 1000
    cr = detect_codex_review(_window(), {}, [], marker)
    assert cr is not None
    assert cr["source"] == "mcp"
    assert cr["codex_session_id"] is None
    # Unmatched rollout is a matching failure, not a hang signal.
    assert cr["stalled"] is False


@pytest.mark.unit
def test_exec_without_rollout_flags_stalled_after_grace():
    # 2026-06-10 Stall A signature: codex exec hung reading stdin BEFORE it
    # ever created a rollout — process alive for hours, no session files.
    exec_map = {100: {"elapsed_s": 6 * 3600, "pid": 200, "command": "codex exec"}}
    cr = detect_codex_review(_window(), exec_map, [], None)
    assert cr["source"] == "exec"
    assert cr["stalled"] is True
    assert cr["stall_reason"] == "no_rollout"
    assert cr["silent_s"] is None


@pytest.mark.unit
def test_exec_without_rollout_healthy_during_startup_grace():
    exec_map = {100: {"elapsed_s": 30, "pid": 200, "command": "codex exec"}}
    cr = detect_codex_review(_window(), exec_map, [], None)
    assert cr["stalled"] is False
    assert cr["stall_reason"] is None


@pytest.mark.unit
def test_stalled_mcp_wins_over_healthy_exec():
    # The 2026-06-10 double stall: a zombie exec child must not mask a
    # concurrently hung MCP call on the same window.
    exec_map = {100: {"elapsed_s": 60, "pid": 200, "command": "codex exec"}}
    marker = NOW_MS - 5400 * 1000
    rolls = [
        _rollout(55, sid="exec-roll", last_write_offset_s=5),
        _rollout(5390, originator="codex_cli_rs", sid="mcp-sid",
                 last_write_offset_s=20 * 60),
    ]
    cr = detect_codex_review(_window(), exec_map, rolls, marker)
    assert cr["source"] == "mcp"
    assert cr["stalled"] is True


@pytest.mark.unit
def test_stalled_exec_wins_over_healthy_mcp():
    exec_map = {100: {"elapsed_s": 7200, "pid": 200, "command": "codex exec"}}
    marker = NOW_MS - 300 * 1000
    rolls = [
        _rollout(7100, sid="exec-roll", last_write_offset_s=3600),
        _rollout(290, originator="codex_cli_rs", sid="mcp-sid",
                 last_write_offset_s=5),
    ]
    cr = detect_codex_review(_window(), exec_map, rolls, marker)
    assert cr["source"] == "exec"
    assert cr["stalled"] is True


@pytest.mark.unit
def test_no_signals_returns_none():
    assert detect_codex_review(_window(), {}, [], None) is None


# ---------- codex_call_marker ----------

def _row(rtype: str, content, ts_ms: int = NOW_MS):
    ts = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(ts_ms / 1000)) + ".000Z"
    return {"type": rtype, "timestamp": ts,
            "message": {"role": rtype, "content": content}}


def _write(tmp_path: Path, rows) -> Path:
    p = tmp_path / "t.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    return p


@pytest.mark.unit
def test_marker_toolsearch_codex_at_tail(tmp_path):
    p = _write(tmp_path, [
        _row("assistant", [{"type": "text", "text": "發起 Codex 審查 Round 1"}]),
        _row("assistant", [{"type": "tool_use", "id": "t1", "name": "ToolSearch",
                            "input": {"query": "select:mcp__codex__codex"}}]),
        _row("user", [{"type": "tool_result", "tool_use_id": "t1", "content": "loaded"}]),
    ])
    assert codex_call_marker(p) is not None


@pytest.mark.unit
def test_marker_inflight_mcp_tool_use(tmp_path):
    p = _write(tmp_path, [
        _row("assistant", [{"type": "tool_use", "id": "t2", "name": "mcp__codex__codex",
                            "input": {"prompt": "review this"}}]),
    ])
    assert codex_call_marker(p) is not None


@pytest.mark.unit
def test_marker_cleared_by_completed_mcp_call(tmp_path):
    p = _write(tmp_path, [
        _row("assistant", [{"type": "tool_use", "id": "t2", "name": "mcp__codex__codex",
                            "input": {"prompt": "review"}}]),
        _row("user", [{"type": "tool_result", "tool_use_id": "t2", "content": "verdict: pass"}]),
    ])
    assert codex_call_marker(p) is None


@pytest.mark.unit
def test_marker_cleared_by_later_action(tmp_path):
    p = _write(tmp_path, [
        _row("assistant", [{"type": "tool_use", "id": "t1", "name": "ToolSearch",
                            "input": {"query": "select:mcp__codex__codex"}}]),
        _row("user", [{"type": "tool_result", "tool_use_id": "t1", "content": "loaded"}]),
        _row("assistant", [{"type": "tool_use", "id": "t3", "name": "Bash",
                            "input": {"command": "ls"}}]),
    ])
    assert codex_call_marker(p) is None


# ---------- rollout_current_action ----------

@pytest.mark.unit
def test_rollout_current_action(tmp_path):
    rows = [
        {"type": "session_meta", "payload": {"id": "x"}},
        {"type": "response_item", "payload": {"type": "function_call", "name": "exec_command",
                                              "arguments": json.dumps({"cmd": "grep -n foo bar.html"})}},
    ]
    p = _write(tmp_path, rows)
    act = rollout_current_action(p)
    assert act is not None and "grep -n foo" in act


# ---------- patrol integration ----------

@pytest.mark.unit
def test_classify_codex_review_working():
    w = {"status": "busy", "idle_seconds": 100, "name": "s", "transcript_path": None,
         "pending_wakeup": None,
         "codex_review": {"source": "mcp", "elapsed_s": 5600, "stalled": False,
                          "silent_s": None, "current_action": None}}
    tri = classify(w)
    assert tri["triage"] == "working"
    assert "Codex reviewing" in tri["reason"]


@pytest.mark.unit
def test_classify_codex_review_stalled():
    w = {"status": "shell", "idle_seconds": 100, "name": "s", "transcript_path": None,
         "pending_wakeup": None,
         "codex_review": {"source": "exec", "elapsed_s": 86000, "stalled": True,
                          "silent_s": 13000, "current_action": "output: yaml"}}
    tri = classify(w)
    assert tri["triage"] == "stalled"
    assert "looks stalled" in tri["reason"]
