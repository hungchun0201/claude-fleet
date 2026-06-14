"""Remote (lab) session parsing + local-attachment detection (no SSH)."""
from __future__ import annotations

import json

import pytest

from core import remote


def _block(obj: dict, transcript: str = "") -> str:
    return (f"{remote._SEP_SESSION}\n{json.dumps(obj)}\n"
            f"{remote._SEP_TRANSCRIPT}\n{transcript}\n{remote._SEP_END}\n")


@pytest.fixture(autouse=True)
def _tmp_root(tmp_path, monkeypatch):
    monkeypatch.setattr(remote, "_TMP_ROOT", tmp_path)


@pytest.mark.unit
def test_parse_keeps_lab_sessions_only():
    stream = (
        _block({"pid": 1, "sessionId": "a", "cwd": "/home/h/x", "name": "lab-foo",
                "status": "idle", "updatedAt": 1}, transcript='{"type":"user"}')
        + _block({"pid": 2, "sessionId": "b", "cwd": "/home/h/y", "name": None,
                  "status": "idle", "updatedAt": 1})
        + _block({"pid": 3, "sessionId": "c", "cwd": "/home/h/z", "name": "other",
                  "status": "busy", "updatedAt": 1})
    )
    out = remote._parse("lab", stream)
    assert [w["name"] for w in out] == ["lab-foo"]
    w = out[0]
    assert w["remote"] is True and w["host"] == "lab"
    assert w["transcript_path"] is not None  # tail was mirrored locally


@pytest.mark.unit
def test_parse_no_transcript_when_tail_empty():
    stream = _block({"pid": 1, "sessionId": "a", "cwd": "/home/h/x", "name": "lab-foo",
                     "status": "idle", "updatedAt": 1}, transcript="")
    assert remote._parse("lab", stream)[0]["transcript_path"] is None


@pytest.mark.unit
def test_local_attachment_pid_matches_claude_lab():
    ps = {
        6408: (3900, "ssh lab -t /home/hclin/.local/bin/claude-lab testttt"),
        3900: (920, "/bin/zsh -il"),
        555: (1, "some other process"),
    }
    assert remote.local_attachment_pid("lab-testttt", ps) == 6408
    assert remote.local_attachment_pid("lab-nope", ps) is None
    assert remote.local_attachment_pid(None, ps) is None
    assert remote.local_attachment_pid("not-lab-prefixed", ps) is None
