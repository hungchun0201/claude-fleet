"""One-shot ntfy push when a window stays in triage "stalled".

check() is a pure state machine over snapshots (no I/O, unit-testable);
push() does the blocking HTTP POST and is meant to be run off the event
loop (asyncio.to_thread) as fire-and-forget.

The stalled flags themselves already embed their own silence thresholds
(15 min rollout silence, 3 min missing rollout, overdue wakeups), so the
debounce here only filters flapping — a push lands a couple of minutes
into a confirmed hang instead of hours later when a human looks.
"""
from __future__ import annotations

import os
import time
import urllib.parse
import urllib.request

STALL_ALERT_AFTER_S = 120
NTFY_TOPIC = os.environ.get("CLAUDE_FLEET_NTFY_TOPIC", "your-ntfy-topic")
NTFY_URL = f"https://ntfy.sh/{NTFY_TOPIC}"

_stall_seen: dict[str, float] = {}   # key -> first tick seen stalled
_alerted: set[str] = set()           # keys already pushed (one-shot)


def _cause(w: dict) -> str:
    """Stable identity of WHY the window is stalled, so a new, different
    stall on the same pid re-alerts while the same ongoing one does not."""
    cr = w.get("codex_review") or {}
    if cr.get("stalled"):
        return f"codex:{cr.get('source')}:{cr.get('codex_session_id') or 'norollout'}"
    wf = w.get("workflow_run") or {}
    if wf.get("stalled"):
        return f"workflow:{wf.get('name') or '?'}"
    pw = w.get("pending_wakeup") or {}
    if pw.get("overdue"):
        return "wakeup-overdue"
    return "generic"


def check(snapshot: dict, now: float | None = None) -> list[dict]:
    """Return alert payloads due this tick; tracks state across ticks.

    A key alerts once after STALL_ALERT_AFTER_S of continuous stall; it
    re-arms when the window recovers (or disappears).
    """
    now = time.time() if now is None else now
    due: list[dict] = []
    live_keys: set[str] = set()
    for w in snapshot.get("windows", []):
        if w.get("triage") != "stalled":
            continue
        key = f"{w.get('pid')}:{_cause(w)}"
        live_keys.add(key)
        first = _stall_seen.setdefault(key, now)
        if key not in _alerted and now - first >= STALL_ALERT_AFTER_S:
            _alerted.add(key)
            name = w.get("name") or w.get("project_name") or f"pid {w.get('pid')}"
            due.append({
                "title": f"Fleet: {name} 卡住",
                "message": (w.get("triage_reason") or "stalled")[:300],
                "tags": "rotating_light",
                "priority": "high",
            })
    for key in list(_stall_seen):
        if key not in live_keys:
            _stall_seen.pop(key, None)
            _alerted.discard(key)
    return due


def push(alert: dict) -> bool:
    """Blocking ntfy POST; never raises (called fire-and-forget).

    Title/tags/priority go in the query string — header values must be
    latin-1 and ours contain CJK.
    """
    qs = urllib.parse.urlencode({
        "title": alert.get("title", "Fleet alert"),
        "tags": alert.get("tags", "warning"),
        "priority": alert.get("priority", "high"),
    })
    try:
        req = urllib.request.Request(
            f"{NTFY_URL}?{qs}",
            data=alert.get("message", "").encode("utf-8"),
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            return 200 <= resp.status < 300
    except Exception as e:
        print(f"[alerts] ntfy push failed: {e}")
        return False
