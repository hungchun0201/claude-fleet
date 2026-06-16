"""Remote (lab) session parsing + local-attachment detection (no SSH)."""
from __future__ import annotations

import json
from pathlib import Path

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


@pytest.mark.unit
def test_poll_distinguishes_empty_from_failure(monkeypatch):
    # SSH succeeded but no lab sessions -> ok=True, [] (clear the cache).
    monkeypatch.setattr(remote, "_ssh_fetch", lambda h: "")
    assert remote.poll() == (True, [])
    # SSH failed -> ok=False (keep last-known, mark stale).
    monkeypatch.setattr(remote, "_ssh_fetch", lambda h: None)
    assert remote.poll() == (False, [])


@pytest.mark.unit
def test_last_event_ms_picks_newest_and_ignores_metadata(tmp_path):
    from core import transcripts
    p = tmp_path / "t.jsonl"
    rows = [
        {"type": "assistant", "timestamp": "2026-06-16T01:00:00.000Z", "message": {}},
        {"type": "user", "timestamp": "2026-06-16T01:05:00.000Z", "message": {}},
        {"type": "mode"},  # bridge sessions append metadata rows with no timestamp
    ]
    p.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    assert transcripts.last_event_ms(p) == transcripts._parse_ts_ms("2026-06-16T01:05:00.000Z")
    # Missing file / timestamp-less transcript -> None.
    assert transcripts.last_event_ms(tmp_path / "nope.jsonl") is None
    meta = tmp_path / "meta.jsonl"
    meta.write_text(json.dumps({"type": "mode"}) + "\n")
    assert transcripts.last_event_ms(meta) is None


@pytest.mark.unit
def test_enrich_remote_idle_from_transcript_for_bridge_session(tmp_path):
    # Regression: a claude-lab bridge session freezes the session-file
    # updatedAt (so idle_seconds climbs) while the transcript keeps growing.
    # _enrich_remote must report idle from the fresh transcript row, not the
    # stale updatedAt — otherwise a live session looks untracked / completed.
    import time
    import app
    iso = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(time.time() - 5)) + ".000Z"
    p = tmp_path / "t.jsonl"
    p.write_text(json.dumps({
        "type": "assistant", "timestamp": iso,
        "message": {"role": "assistant", "stop_reason": "tool_use",
                    "content": [{"type": "tool_use", "id": "x", "name": "Read", "input": {}}]},
    }) + "\n")
    rw = {"pid": 1, "name": "lab-x", "status": "busy", "idle_seconds": 600,
          "updated_at": 0, "transcript_path": str(p)}
    w = app._enrich_remote(rw, None, stale=False)
    assert w["idle_seconds"] <= 30  # from the ~5s-old transcript row, not 600
    assert w["triage"] == "working"


@pytest.mark.unit
def test_fetch_full_transcript_writes_mirror(tmp_path, monkeypatch):
    monkeypatch.setattr(remote, "_TMP_ROOT", tmp_path)
    monkeypatch.setattr(remote, "_ssh_cat",
                        lambda host, slug, sid: '{"type":"user"}\n{"type":"assistant"}')
    fp = remote.fetch_full_transcript("lab", "/home/h/proj", "sid1")
    assert fp is not None and Path(fp).exists()
    assert "full" in fp and fp.endswith("sid1.jsonl")
    # Host unreachable / missing file -> None. Missing args -> None.
    monkeypatch.setattr(remote, "_ssh_cat", lambda *a: None)
    assert remote.fetch_full_transcript("lab", "/home/h/proj", "sid1") is None
    assert remote.fetch_full_transcript(None, "/home/h/proj", "sid1") is None


@pytest.mark.unit
def test_remote_timeline_endpoint_full_then_tail_fallback(tmp_path, monkeypatch):
    # The lab-card timeline SSH-fetches the full transcript; if the host is
    # unreachable it falls back to the mirrored tail, flagged partial.
    import app
    import fastapi
    monkeypatch.setattr(remote, "_TMP_ROOT", tmp_path)
    host = tmp_path / "lab"
    host.mkdir()
    tail = host / "sidX.jsonl"
    tail.write_text(json.dumps({
        "type": "assistant", "timestamp": "2026-06-16T01:00:00.000Z",
        "message": {"role": "assistant", "content": [{"type": "text", "text": "tail event"}]},
    }) + "\n")
    rw = {"pid": 42, "host": "lab", "cwd": "/home/h/p", "session_id": "sidX",
          "transcript_path": str(tail)}
    monkeypatch.setitem(app._lab_cache, "windows", [rw])

    # Full fetch succeeds -> complete history, not partial.
    full_body = json.dumps({
        "type": "assistant", "timestamp": "2026-06-16T02:00:00.000Z",
        "message": {"role": "assistant", "content": [{"type": "text", "text": "full history"}]},
    }) + "\n"
    monkeypatch.setattr(remote, "_ssh_cat", lambda *a: full_body)
    res = app.api_remote_timeline(42)
    assert res["partial"] is False
    assert any("full history" in (e.get("text") or "") for e in res["events"])

    # Host unreachable -> partial tail fallback.
    monkeypatch.setattr(remote, "_ssh_cat", lambda *a: None)
    res2 = app.api_remote_timeline(42)
    assert res2["partial"] is True
    assert any("tail event" in (e.get("text") or "") for e in res2["events"])

    # Unknown pid -> 404.
    with pytest.raises(fastapi.HTTPException) as ei:
        app.api_remote_timeline(999)
    assert ei.value.status_code == 404
