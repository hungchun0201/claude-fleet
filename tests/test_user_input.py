"""first_user_input / last_user_input — clean genuine prompts, noise filtered."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from core import transcripts


def _u(text):
    return {"type": "user", "message": {"role": "user", "content": text}}


def _u_blocks(*blocks):
    return {"type": "user", "message": {"role": "user", "content": list(blocks)}}


def _write(tmp_path: Path, rows: list[dict]) -> str:
    p = tmp_path / "t.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    return str(p)


@pytest.mark.unit
def test_last_user_input_skips_command_output(tmp_path):
    rows = [
        _u("開始做 systemd"),
        _u("<local-command-stdout>Set model to Opus</local-command-stdout>"),
        _u("<command-name>/model</command-name>"),
    ]
    assert transcripts.last_user_input(_write(tmp_path, rows)) == "開始做 systemd"


@pytest.mark.unit
def test_strips_appended_system_reminder(tmp_path):
    rows = [_u("實作這個功能\n<system-reminder>CLAUDE.md ...</system-reminder>")]
    assert transcripts.last_user_input(_write(tmp_path, rows)) == "實作這個功能"


@pytest.mark.unit
def test_skips_tool_result_messages(tmp_path):
    rows = [_u("真的輸入"),
            _u_blocks({"type": "tool_result", "content": "stdout..."})]
    assert transcripts.last_user_input(_write(tmp_path, rows)) == "真的輸入"


@pytest.mark.unit
def test_first_user_input_skips_caveat_wrapper(tmp_path):
    # The very first message is a slash-command caveat wrapper — not a title.
    rows = [
        _u("<local-command-caveat>Caveat: The messages below were generated…"),
        _u("開始做systemd 開機拉回來"),
    ]
    assert transcripts.first_user_input(_write(tmp_path, rows)) == "開始做systemd 開機拉回來"


@pytest.mark.unit
def test_typeahead_in_queue_is_most_recent(tmp_path):
    # The user typed two messages while the assistant was busy → they live in
    # queue-operation enqueue rows, more recent than the last processed turn.
    rows = [
        _u("舊的已處理輸入"),
        {"type": "queue-operation", "operation": "enqueue", "content": "有個問題 …"},
        {"type": "queue-operation", "operation": "enqueue", "content": "希望不要和斷線搞混"},
        {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": "..."}]}},
        _u_blocks({"type": "tool_result", "content": "stdout"}),  # noise after the queue
    ]
    # walking back, the latest genuine input is the most-recent enqueue
    assert transcripts.last_user_input(_write(tmp_path, rows)) == "希望不要和斷線搞混"


@pytest.mark.unit
def test_queue_task_notification_is_not_user_input(tmp_path):
    rows = [
        _u("真的輸入"),
        {"type": "queue-operation", "operation": "enqueue",
         "content": "<task-notification><status>completed</status></task-notification>"},
    ]
    assert transcripts.last_user_input(_write(tmp_path, rows)) == "真的輸入"


@pytest.mark.unit
def test_no_genuine_input_returns_none(tmp_path):
    # A session that only ran slash commands has no typed prompt.
    rows = [_u("<command-name>/rename test</command-name>"),
            _u("<local-command-stdout>renamed</local-command-stdout>")]
    assert transcripts.last_user_input(_write(tmp_path, rows)) is None
    assert transcripts.first_user_input(_write(tmp_path, rows)) is None
