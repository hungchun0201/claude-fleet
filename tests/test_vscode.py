"""VS Code integrated-terminal detection (shell pid resolution) and reattach."""
from __future__ import annotations

import json

import pytest

from core import vscode

HELPER = ("/Applications/Visual Studio Code 2.app/Contents/Frameworks/"
          "Code Helper.app/Contents/MacOS/Code Helper --type=utility")
MAIN = "/Applications/Visual Studio Code 2.app/Contents/MacOS/Code"


@pytest.mark.unit
def test_detect_direct_zsh_shell():
    # claude -> zsh -> Code Helper(Frameworks) -> Code main -> launchd
    info = {
        56834: (56543, "claude"),
        56543: (920, "/bin/zsh -il"),
        920: (604, HELPER),
        604: (1, MAIN),
        1: (0, "/sbin/launchd"),
    }
    out = vscode.detect(56834, info)
    assert out == {"shell_pid": 56543, "app": "Visual Studio Code 2"}


@pytest.mark.unit
def test_detect_claude_directly_under_host():
    # No separate shell: the shell pid is claude's own pid.
    info = {
        100: (920, "claude"),
        920: (604, HELPER),
        604: (1, MAIN),
    }
    assert vscode.detect(100, info)["shell_pid"] == 100


@pytest.mark.unit
def test_detect_non_vscode_returns_none():
    # A plain Terminal.app session has no Frameworks pty-host ancestor.
    info = {
        300: (290, "claude"),
        290: (1, "login -fp user"),
        1: (0, "/sbin/launchd"),
    }
    assert vscode.detect(300, info) is None


@pytest.mark.unit
def test_app_name_extraction():
    assert vscode._app_name("/Applications/Cursor.app/Contents/Frameworks/x") == "Cursor"
    assert vscode._app_name("/x/VSCodium.app/Contents/MacOS/Electron") == "VSCodium"
    assert vscode._app_name("/usr/bin/zsh") is None


@pytest.mark.unit
def test_ps_parents_survives_non_utf8_command(monkeypatch):
    # Regression: one process with a non-UTF-8 byte in its command line (here
    # 0xba) must not blow up the whole table. Strict decode raised
    # UnicodeDecodeError -> _ps_parents returned {} -> every focus / shell-pid /
    # attachment lookup silently failed. The fix decodes with errors="replace".
    raw = b"100 1 /sbin/launchd\n200 100 /Applications/Caf\xbe.app/x\n"

    def fake_check_output(cmd, **kw):
        # Emulate subprocess decoding the raw bytes with the requested handler.
        return raw.decode("utf-8", errors=kw.get("errors", "strict"))

    monkeypatch.setattr(vscode.subprocess, "check_output", fake_check_output)
    info = vscode._ps_parents()
    assert len(info) == 2  # strict decode would have made this {}
    assert info[200][0] == 100


# --------------------------- reattach helpers --------------------------- #


@pytest.mark.unit
def test_queue_reattach_job_appends_and_expires(tmp_path, monkeypatch):
    monkeypatch.setattr(vscode, "REATTACH_FILE", tmp_path / "vscode-reattach")
    monkeypatch.setattr(vscode, "REATTACH_CLAIMS_DIR", tmp_path / "claims")
    # A stale job (older than the TTL) must be dropped when a new one is queued.
    old_ts = 1_000_000
    vscode._write_reattach_jobs([{"id": "old", "cmd": "x", "label": "old", "ts": old_ts}])
    now = old_ts + (vscode.REATTACH_TTL_S + 5) * 1000
    jid = vscode.queue_reattach_job("claude-lab foo", "lab-foo", now_ms=now)

    jobs = vscode._read_reattach_jobs()
    assert [j["id"] for j in jobs] == [jid]
    assert jobs[0]["cmd"] == "claude-lab foo"
    assert jobs[0]["ts"] == now
    # id is filename-safe (used as a claim-marker filename by the extension).
    assert "/" not in jid and " " not in jid


@pytest.mark.unit
def test_queue_reattach_job_drops_consumed_job(tmp_path, monkeypatch):
    monkeypatch.setattr(vscode, "REATTACH_FILE", tmp_path / "vscode-reattach")
    claims = tmp_path / "claims"
    claims.mkdir()
    monkeypatch.setattr(vscode, "REATTACH_CLAIMS_DIR", claims)
    # A still-fresh job that already has a claim marker (a window ran it) must be
    # dropped on the next enqueue, so it can never be re-run by a later window.
    now = 5_000_000
    vscode._write_reattach_jobs([{"id": "done", "cmd": "x", "label": "d", "ts": now - 1000}])
    (claims / "done").write_text("claimed")
    jid = vscode.queue_reattach_job("claude-lab bar", "lab-bar", now_ms=now)
    assert [j["id"] for j in vscode._read_reattach_jobs()] == [jid]  # 'done' pruned


@pytest.mark.unit
def test_reattach_claimed(tmp_path, monkeypatch):
    claims = tmp_path / "claims"
    claims.mkdir()
    monkeypatch.setattr(vscode, "REATTACH_CLAIMS_DIR", claims)
    assert vscode.reattach_claimed("1781-1-lab-foo") is False  # no marker yet
    (claims / "1781-1-lab-foo").write_text("x")
    assert vscode.reattach_claimed("1781-1-lab-foo") is True    # window claimed it
    # An unsafe / empty id never touches the filesystem.
    assert vscode.reattach_claimed("../../etc/passwd") is False
    assert vscode.reattach_claimed("") is False


@pytest.mark.unit
def test_reattach_remote_rejects_unsafe_suffix(monkeypatch):
    out = vscode.reattach_remote("foo; rm -rf ~")
    assert out["ok"] is False and "unsafe" in out["error"]


@pytest.mark.unit
def test_reattach_remote_queues_job(tmp_path, monkeypatch):
    monkeypatch.setattr(vscode, "REATTACH_FILE", tmp_path / "vscode-reattach")
    monkeypatch.setattr(vscode, "REATTACH_CLAIMS_DIR", tmp_path / "claims")

    out = vscode.reattach_remote("agent-kvcache-review", label="lab-agent-kvcache-review",
                                 host="lab")
    assert out["ok"] is True and out["via"] == "vscode-reattach"
    assert out["cmd"] == "claude-lab agent-kvcache-review"
    # The queued job the focused window will run carries the exact command.
    data = json.loads((tmp_path / "vscode-reattach").read_text())
    assert data["jobs"][0]["cmd"] == "claude-lab agent-kvcache-review"
    assert data["jobs"][0]["label"] == "lab-agent-kvcache-review"
