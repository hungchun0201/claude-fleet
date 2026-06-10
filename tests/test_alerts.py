"""Tests for one-shot stalled-session ntfy alerting (core/alerts.py)."""
from __future__ import annotations

import pytest

from core import alerts


@pytest.fixture(autouse=True)
def _reset_state():
    alerts._stall_seen.clear()
    alerts._alerted.clear()
    yield
    alerts._stall_seen.clear()
    alerts._alerted.clear()


def _snap(*windows):
    return {"windows": list(windows)}


def _stalled_window(pid=100, name="paper-review", sid="sid-1"):
    return {
        "pid": pid, "name": name, "triage": "stalled",
        "triage_reason": "Codex 審查疑似卡死：已 6h36m，輸出停滯 6h30m",
        "codex_review": {"source": "mcp", "stalled": True, "codex_session_id": sid},
    }


def _working_window(pid=100, name="paper-review"):
    return {"pid": pid, "name": name, "triage": "working",
            "triage_reason": "工作中", "codex_review": None}


@pytest.mark.unit
def test_no_alert_before_debounce():
    assert alerts.check(_snap(_stalled_window()), now=1000.0) == []
    assert alerts.check(_snap(_stalled_window()), now=1060.0) == []


@pytest.mark.unit
def test_alert_fires_once_after_debounce():
    alerts.check(_snap(_stalled_window()), now=1000.0)
    due = alerts.check(_snap(_stalled_window()), now=1000.0 + alerts.STALL_ALERT_AFTER_S)
    assert len(due) == 1
    assert "paper-review" in due[0]["title"]
    assert "卡死" in due[0]["message"]
    # Stays stalled -> no repeat.
    assert alerts.check(_snap(_stalled_window()), now=2000.0) == []
    assert alerts.check(_snap(_stalled_window()), now=9000.0) == []


@pytest.mark.unit
def test_recovery_rearms():
    alerts.check(_snap(_stalled_window()), now=1000.0)
    assert alerts.check(_snap(_stalled_window()), now=1200.0)
    # Recovers...
    alerts.check(_snap(_working_window()), now=1300.0)
    # ...then stalls again: full debounce + a fresh alert.
    assert alerts.check(_snap(_stalled_window()), now=1400.0) == []
    assert len(alerts.check(_snap(_stalled_window()), now=1400.0 + alerts.STALL_ALERT_AFTER_S)) == 1


@pytest.mark.unit
def test_distinct_causes_alert_separately():
    w1 = _stalled_window(pid=100, sid="sid-1")
    alerts.check(_snap(w1), now=1000.0)
    assert len(alerts.check(_snap(w1), now=1200.0)) == 1
    # Same pid, NEW codex session hang -> new key -> alerts again.
    w2 = _stalled_window(pid=100, sid="sid-2")
    alerts.check(_snap(w2), now=1300.0)
    assert len(alerts.check(_snap(w2), now=1300.0 + alerts.STALL_ALERT_AFTER_S)) == 1


@pytest.mark.unit
def test_multiple_windows_each_alert():
    a = _stalled_window(pid=100, name="s-a", sid="sid-a")
    b = _stalled_window(pid=200, name="s-b", sid="sid-b")
    alerts.check(_snap(a, b), now=1000.0)
    due = alerts.check(_snap(a, b), now=1200.0)
    assert len(due) == 2
