"""Triage classifier: inspect each session's transcript to determine its state."""
from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Optional

IDLE_THRESHOLD = 300     # 5 min
CLOSEABLE_THRESHOLD = 3600  # 1 hour

TRIAGE_PRIORITY = {
    "waiting_perm": 0,
    "stalled": 1,
    "completed": 2,
    "working": 3,
    "closeable": 4,
}


def _last_assistant_info(transcript_path: str) -> Optional[dict]:
    """Extract stop_reason, last content block type, and background task status."""
    p = Path(transcript_path)
    if not p.exists():
        return None
    lines: list[str] = []
    try:
        with p.open() as f:
            for line in f:
                lines.append(line)
    except Exception:
        return None

    # NOTE: we deliberately do NOT infer background work from queue-operation
    # events. Those carry the user-input / task-notification queue (typeahead, a
    # /slash command, or the delivery of a *completed* background task's
    # notification) — an "enqueue"/"dequeue" after end_turn does not mean work is
    # running, and treating it as such flipped finished sessions to "working".
    # Real background work is detected structurally via extract_background_tasks.

    # Find the last assistant message for stop_reason etc.
    stop_reason = ""
    last_block_type = ""
    last_text = ""
    last_tool = ""
    api_error = False
    for raw in reversed(lines[-40:]):
        try:
            d = json.loads(raw)
        except Exception:
            continue
        if d.get("type") != "assistant":
            continue
        msg = d.get("message") or {}
        content = msg.get("content") or []
        stop_reason = msg.get("stop_reason", "")
        # Claude Code records API failures (model unavailable, no access, …) as
        # a synthetic assistant row flagged isApiErrorMessage. Such a turn has
        # no clean end_turn/tool_use stop_reason, so without flagging it here
        # classify() would fall through to the idle<5min catch-all and wrongly
        # read "working" for a session that is actually stuck waiting on /model.
        api_error = bool(d.get("isApiErrorMessage")) or msg.get("model") == "<synthetic>"
        if isinstance(content, list) and content:
            last_block = content[-1]
            last_block_type = last_block.get("type", "")
            if last_block_type == "text":
                last_text = last_block.get("text", "")
            elif last_block_type == "tool_use":
                last_tool = last_block.get("name", "")
            for c in reversed(content):
                if c.get("type") == "text" and c.get("text", "").strip():
                    last_text = c["text"].strip()
                    break
        break

    # NOTE: no keyword fallback on last_text here. Merely MENTIONING the word
    # "background" (in any language) in prose (e.g. a summary quoting a spawn
    # ack) used to flip an idle session to working. Real background work is detected
    # structurally: active tasks via extract_background_tasks (classify reads
    # window_dict["background_tasks"]) and queued notifications above.
    return {
        "stop_reason": stop_reason,
        "last_block_type": last_block_type,
        "last_text": last_text[:200],
        "last_tool": last_tool,
        "api_error": api_error,
    }


def classify(window_dict: dict) -> dict:
    """Classify a window dict (from sessions.snapshot) into a triage state.

    Returns {triage, reason, suggestion}.
    """
    status = window_dict.get("status", "unknown")
    idle = window_dict.get("idle_seconds", 0)
    name = window_dict.get("name") or window_dict.get("project_name") or ""
    transcript = window_dict.get("transcript_path")

    if status == "waiting":
        return {
            "triage": "waiting_perm",
            "reason": window_dict.get("waiting_for") or "Awaiting permission",
            "suggestion": "Approve in the terminal",
        }

    # Session is running a Codex review (exec child process or in-flight MCP
    # call). Stalled = exec rollout silent too long (likely hung).
    cr = window_dict.get("codex_review")
    if cr:
        elapsed = _format_idle(cr.get("elapsed_s") or 0)
        if cr.get("stalled"):
            if cr.get("stall_reason") == "no_rollout":
                reason = f"Codex review looks stalled: process up {elapsed} but never wrote a rollout (classic stdin hang)"
            else:
                silent = _format_idle(cr.get("silent_s") or 0)
                reason = f"Codex review looks stalled: {elapsed} elapsed, output silent {silent}"
            return {
                "triage": "stalled",
                "reason": reason,
                "suggestion": "Check Codex progress; consider restarting the review",
            }
        act = (cr.get("current_action") or "")[:70]
        tail = f" · {act}" if act else (" (MCP call, no live output)" if cr.get("source") == "mcp" else "")
        return {
            "triage": "working",
            "reason": f"Codex reviewing · {elapsed} elapsed{tail}",
            "suggestion": "",
        }

    # A live Workflow run: multi-agent fan-out executing in the background.
    # The main turn usually ends right after launching it, so without this
    # the card would read "completed" while dozens of agents still work.
    wf = window_dict.get("workflow_run")
    if wf:
        name = wf.get("name") or "workflow"
        elapsed = _format_idle(wf.get("elapsed_s") or 0)
        prog = ""
        if wf.get("agents_started"):
            prog = f", agents {wf.get('agents_done') or 0}/{wf['agents_started']} done"
        if wf.get("stalled"):
            silent = _format_idle(wf.get("silent_s") or 0)
            return {
                "triage": "stalled",
                "reason": f"Workflow {name} looks stalled: {elapsed} elapsed{prog}, no new output {silent}",
                "suggestion": "Use /workflows to check progress; TaskStop and resume if needed",
            }
        return {
            "triage": "working",
            "reason": f"Workflow {name} running · {elapsed} elapsed{prog}",
            "suggestion": "",
        }

    # Session is waiting on GPU work: either sleeping on a ScheduleWakeup
    # (has a concrete wake time) or running a background waiter (poll loop).
    # Without this it would read as generic "working" / "stalled at ScheduleWakeup".
    pw = window_dict.get("pending_wakeup")
    if pw:
        label = "Waiting on GPU" if pw.get("kind") == "gpu" else "Scheduled wake"
        why = (pw.get("reason") or "").split("\n")[0][:80]
        wake_ms = pw.get("wake_at_ms")
        if wake_ms:
            wake_hhmm = time.strftime("%H:%M", time.localtime(wake_ms / 1000))
            if pw.get("overdue"):
                return {
                    "triage": "stalled",
                    "reason": f"{label}, wake overdue (was due {wake_hhmm}). {why}",
                    "suggestion": "Check whether the session is stuck",
                }
            return {
                "triage": "working",
                "reason": f"{label} · next wake {wake_hhmm}. {why}",
                "suggestion": "",
            }
        itv = pw.get("poll_interval_s")
        if itv and itv >= 60:
            cadence = f"bg waiter checks every ~{itv // 60}m"
        elif itv:
            cadence = f"bg waiter checks every {itv}s"
        else:
            cadence = "bg waiter monitoring"
        return {
            "triage": "working",
            "reason": f"{label} ({cadence}). {why}",
            "suggestion": "",
        }

    if status == "busy" and idle < IDLE_THRESHOLD:
        return {
            "triage": "working",
            "reason": "Working",
            "suggestion": "",
        }

    # NOTE: status == "shell" is NOT treated as working. It means the turn has
    # already ended (end_turn) and the agent is idle — only a background shell
    # lingers (often a forgotten server or a bare `&` that never exits). We fall
    # through to the transcript-based logic, so such a session reads "completed"
    # (or "working" if a tracked run_in_background task is genuinely active, via
    # the background_tasks check below). The lingering shell is surfaced as a
    # 🐚 badge on the card (driven by w.status) rather than masking completion.

    if not transcript:
        return {
            "triage": "closeable",
            "reason": "No transcript",
            "suggestion": "Safe to close",
        }

    info = _last_assistant_info(transcript)
    if not info:
        return {
            "triage": "closeable",
            "reason": "Transcript is empty",
            "suggestion": "Safe to close",
        }

    # API failure at the tail (e.g. the selected model was disabled / revoked):
    # the turn aborted and the session sits waiting for the user to pick a model.
    # This must win over the idle<5min catch-all below, which would mislabel it
    # "working" even though nothing is running.
    if info.get("api_error"):
        txt = (info.get("last_text") or "").strip()
        m = re.search(r"model \(([^)]+)\)", txt)
        model = m.group(1) if m else ""
        low = txt.lower()
        if "model" in low and ("exist" in low or "access" in low or "/model" in low):
            who = f"Model {model}" if model else "The selected model"
            return {
                "triage": "stalled",
                "reason": f"{who} is unavailable, turn aborted (model disabled?) — reselect with /model in the terminal",
                "suggestion": "Run /model in the terminal to pick a usable model, then resend",
            }
        short = txt.split("\n")[0][:100] or "unknown error"
        return {
            "triage": "stalled",
            "reason": f"API error aborted: {short}",
            "suggestion": "Check the terminal error; retry or switch model",
        }

    stop = info["stop_reason"]
    idle_str = _format_idle(idle)

    # Active background tasks (bg Bash / Monitor / Workflow), detected
    # structurally from the transcript. Non-GPU tasks (e.g. a du scan over
    # ssh) land here; GPU waiters were already handled as pending_wakeup.
    bg = window_dict.get("background_tasks") or []
    if bg:
        latest = bg[-1]
        what = (latest.get("description") or latest.get("command") or "")[:60]
        count = f"{len(bg)} background tasks" if len(bg) > 1 else "Background task"
        return {
            "triage": "working",
            "reason": f"{count} running: {what}",
            "suggestion": "",
        }

    if stop == "end_turn":
        summary = info["last_text"].split("\n")[0][:80] if info["last_text"] else ""
        if idle >= CLOSEABLE_THRESHOLD:
            return {
                "triage": "closeable",
                "reason": f"Done, idle {idle_str}. {summary}",
                "suggestion": "Safe to close",
            }
        return {
            "triage": "completed",
            "reason": f"Done, idle {idle_str}. {summary}",
            "suggestion": "Review suggested",
        }

    if stop == "tool_use":
        tool = info["last_tool"]
        if status == "busy":
            return {
                "triage": "working",
                "reason": f"Running {tool}" if tool else "Working",
                "suggestion": "",
            }
        return {
            "triage": "stalled",
            "reason": f"Stalled at {tool}, idle {idle_str}" if tool else f"Stopped midway, idle {idle_str}",
            "suggestion": "Needs user attention",
        }

    # Fallback
    if idle >= CLOSEABLE_THRESHOLD:
        return {
            "triage": "closeable",
            "reason": f"Idle {idle_str}",
            "suggestion": "Safe to close",
        }
    return {
        "triage": "completed" if idle >= IDLE_THRESHOLD else "working",
        "reason": f"Idle {idle_str}",
        "suggestion": "",
    }


def _format_idle(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        h = seconds // 3600
        m = (seconds % 3600) // 60
        return f"{h}h{m}m" if m else f"{h}h"
    # Past a day, switch to day+hour granularity ("2d 3h") instead of letting the
    # hour count run unbounded ("51h") — easier to read at a glance on stale cards.
    d = seconds // 86400
    h = (seconds % 86400) // 3600
    return f"{d}d {h}h" if h else f"{d}d"
