"""Live background-shell detection for status=='shell' sessions."""
from __future__ import annotations

import pytest

from core import shells

SNAP = "/bin/zsh -c source /Users/x/.claude/shell-snapshots/snapshot-zsh-123.sh && setopt X && "
SESSION = 1000


def _zsh(cmd: str) -> str:
    return SNAP + cmd


@pytest.mark.unit
def test_finds_shell_and_its_child_program():
    rows = [
        (SESSION, 1, "29:00", "claude --whatever"),
        (2001, SESSION, "15:00", _zsh("run-server")),               # the lingering shell
        (2002, 2001, "15:00", "/Apps/Chrome --headless=new --foo"),  # what it runs
        (3001, SESSION, "29:00", "node /opt/bin/codex mcp-server"),   # MCP infra: excluded
    ]
    out = shells.background_shells(SESSION, rows=rows)
    assert len(out) == 1
    assert out[0]["pid"] == 2001
    assert "Chrome --headless=new" in out[0]["doing"]
    assert out[0]["elapsed"] == "15:00"


@pytest.mark.unit
def test_idle_shell_has_no_doing():
    rows = [
        (SESSION, 1, "29:00", "claude"),
        (2001, SESSION, "40:00", _zsh("a-finished-command")),  # alive but no child
    ]
    out = shells.background_shells(SESSION, rows=rows)
    assert len(out) == 1
    assert out[0]["doing"] is None


@pytest.mark.unit
def test_pipeline_picks_most_informative_child():
    rows = [
        (SESSION, 1, "29:00", "claude"),
        (2001, SESSION, "15:00", _zsh("pipe")),
        (2002, 2001, "15:00", "tail -5"),                        # short, less useful
        (2003, 2001, "15:00", "uvicorn app:app --port 7903 --reload"),  # the real program
    ]
    out = shells.background_shells(SESSION, rows=rows)
    assert "uvicorn app:app" in out[0]["doing"]


@pytest.mark.unit
def test_nested_shell_not_counted_as_child_program():
    # A child that is itself a snapshot-zsh shell must not be reported as "doing".
    rows = [
        (SESSION, 1, "29:00", "claude"),
        (2001, SESSION, "15:00", _zsh("outer")),
        (2002, 2001, "15:00", _zsh("inner")),  # nested shell, no real program
    ]
    out = shells.background_shells(SESSION, rows=rows)
    # 2001 has only a nested-shell child → idle; 2002 also reported, also idle.
    by_pid = {s["pid"]: s for s in out}
    assert by_pid[2001]["doing"] is None


@pytest.mark.unit
def test_no_rows_returns_empty():
    assert shells.background_shells(SESSION, rows=[]) == []


@pytest.mark.unit
def test_only_infra_children_returns_empty():
    rows = [
        (SESSION, 1, "29:00", "claude"),
        (3001, SESSION, "29:00", "uv --directory /x run src/main.py"),  # no snapshot sig
        (3002, SESSION, "29:00", "node /opt/bin/oracle-mcp"),
    ]
    assert shells.background_shells(SESSION, rows=rows) == []
