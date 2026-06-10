"""Parse ~/.claude/projects/{slug}/{sessionId}.jsonl transcripts."""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional


@dataclass
class TurnEvent:
    ts: str
    kind: str            # user_text | assistant_text | tool_use | tool_result | system
    text: str            # ≤ 4 KB excerpt
    tool: Optional[str]  # name of tool when kind == tool_use
    role: str            # user | assistant | system
    extra: dict          # small structured payload (e.g. tool input keys)


def _iter_lines(path: Path) -> Iterable[dict]:
    try:
        with path.open() as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except Exception:
                    continue
    except FileNotFoundError:
        return


def _tail_lines(path: Path, n: int) -> list[dict]:
    """Last n parsed jsonl rows, reading only the file tail.

    Seeks backwards from EOF in growing blocks instead of streaming the whole
    file — transcripts grow to tens of MB and this runs in the 2s poll loop.
    """
    try:
        with path.open("rb") as f:
            f.seek(0, 2)
            pos = f.tell()
            block = 64 * 1024
            buf = b""
            # +2: margin for a partial first line and a trailing newline.
            while pos > 0 and buf.count(b"\n") < n + 2:
                step = min(block, pos)
                pos -= step
                f.seek(pos)
                buf = f.read(step) + buf
                block *= 2
    except OSError:
        return []
    parts = buf.split(b"\n")
    if pos > 0:
        parts = parts[1:]  # first part may be a partial line
    out: list[dict] = []
    for raw in parts:
        raw = raw.strip()
        if not raw:
            continue
        try:
            d = json.loads(raw)
        except Exception:
            continue
        if isinstance(d, dict):
            out.append(d)
    return out[-n:]


def _flatten_assistant(msg: dict) -> list[TurnEvent]:
    out: list[TurnEvent] = []
    content = msg.get("content") or []
    ts = msg.get("timestamp") or ""
    if isinstance(content, str):
        out.append(TurnEvent(ts, "assistant_text", content[:4000], None, "assistant", {}))
        return out
    if not isinstance(content, list):
        return out
    for c in content:
        ct = c.get("type")
        if ct == "text":
            out.append(TurnEvent(ts, "assistant_text", (c.get("text") or "")[:4000], None, "assistant", {}))
        elif ct == "tool_use":
            inp = c.get("input") or {}
            tool_name = c.get("name", "")
            file_path = str(inp.get("file_path", ""))

            if tool_name == "Skill":
                skill_name = inp.get("skill", "")
                out.append(TurnEvent(
                    ts, "skill_invoke", "", skill_name, "assistant",
                    {"args": (inp.get("args") or "")[:200]},
                ))
            elif tool_name in ("Read", "Write", "Edit") and "/memory/" in file_path:
                mem_name = file_path.rsplit("/", 1)[-1].replace(".md", "")
                kind = "memory_write" if tool_name in ("Write", "Edit") else "memory_read"
                out.append(TurnEvent(
                    ts, kind, "", mem_name, "assistant",
                    {"operation": tool_name.lower(), "path": file_path},
                ))
            else:
                preview: dict = {}
                for k, v in (inp.items() if isinstance(inp, dict) else []):
                    if isinstance(v, str):
                        preview[k] = v[:200]
                    elif isinstance(v, (int, float, bool)) or v is None:
                        preview[k] = v
                    else:
                        preview[k] = f"<{type(v).__name__}>"
                    if len(preview) >= 6:
                        break
                out.append(TurnEvent(ts, "tool_use", "", tool_name, "assistant", preview))
        elif ct == "thinking":
            # Skip thinking — too noisy for dashboard.
            continue
    return out


def _flatten_user(msg: dict) -> list[TurnEvent]:
    out: list[TurnEvent] = []
    content = msg.get("content") or []
    ts = msg.get("timestamp") or ""
    if isinstance(content, str):
        out.append(TurnEvent(ts, "user_text", content[:4000], None, "user", {}))
        return out
    if not isinstance(content, list):
        return out
    for c in content:
        ct = c.get("type")
        if ct == "text":
            out.append(TurnEvent(ts, "user_text", (c.get("text") or "")[:4000], None, "user", {}))
        elif ct == "tool_result":
            # Sensitive: don't dump full stdout. Just first 200 chars.
            content_val = c.get("content")
            if isinstance(content_val, list):
                text_parts = [x.get("text", "") for x in content_val if isinstance(x, dict)]
                snippet = " ".join(text_parts)[:200]
            else:
                snippet = str(content_val)[:200]
            out.append(TurnEvent(ts, "tool_result", snippet, None, "user", {}))
    return out


def _normalize(d: dict) -> list[TurnEvent]:
    t = d.get("type")
    msg = d.get("message") or {}
    # `timestamp` lives on the outer envelope, not inside `message`.
    if msg and "timestamp" not in msg and d.get("timestamp"):
        msg["timestamp"] = d.get("timestamp")
    if t == "assistant":
        return _flatten_assistant(msg)
    if t == "user":
        return _flatten_user(msg)
    if t in {"system", "permission-mode"}:
        return [TurnEvent(
            d.get("timestamp", ""), "system",
            t + (": " + str(d.get("permissionMode", "")) if d.get("permissionMode") else ""),
            None, "system", {}
        )]
    return []


def timeline(path: str | Path, limit: int = 50) -> list[dict]:
    """Return ≤ limit most recent flattened turn events for a transcript."""
    p = Path(path)
    if not p.exists():
        return []
    # Read more lines than needed because one jsonl row can expand into several events.
    raw = _tail_lines(p, max(limit * 2, 100))
    events: list[TurnEvent] = []
    for d in raw:
        events.extend(_normalize(d))
    return [e.__dict__ for e in events[-limit:]]


def current_task_hint(path: str | Path) -> Optional[str]:
    """Best-effort one-liner of what this session is currently doing."""
    p = Path(path)
    if not p.exists():
        return None
    raw = _tail_lines(p, 30)
    # Walk back to the most informative event.
    for d in reversed(raw):
        for ev in reversed(_normalize(d)):
            if ev.kind == "tool_use" and ev.tool:
                key_args = ", ".join(f"{k}={v!r}" for k, v in list(ev.extra.items())[:2])
                return f"{ev.tool}({key_args})" if key_args else ev.tool
            if ev.kind == "assistant_text" and ev.text.strip():
                first = ev.text.strip().splitlines()[0]
                return first[:160]
            if ev.kind == "user_text" and ev.text.strip():
                first = ev.text.strip().splitlines()[0]
                return f"↳ {first[:160]}"
    return None


def extract_skills_used(path: str | Path) -> list[str]:
    """Extract unique skill names invoked via the Skill tool."""
    counts = count_skill_invocations(path)
    return list(counts.keys())


def count_skill_invocations(path: str | Path) -> dict[str, int]:
    """Count total invocations per skill (not deduplicated)."""
    activity = count_skill_activity(path)
    return activity.get("per_skill_invokes", {})


def count_skill_activity(path: str | Path) -> dict:
    """Count all skill-related activity: invocations + file ops + bash refs.

    Returns {
        per_skill_invokes: {name: count},
        per_skill_file_ops: {name: count},
        per_skill_bash_refs: {name: count},
        totals: {invoke, file_ops, bash_refs, total},
    }
    """
    import re
    p = Path(path)
    if not p.exists():
        return {"per_skill_invokes": {}, "per_skill_file_ops": {},
                "per_skill_reads": {}, "per_skill_writes": {},
                "per_skill_bash_refs": {}, "totals": {"invoke": 0, "file_ops": 0, "reads": 0, "writes": 0, "bash_refs": 0, "total": 0}}

    invokes: dict[str, int] = {}
    file_ops: dict[str, int] = {}
    skill_reads: dict[str, int] = {}
    skill_writes: dict[str, int] = {}
    bash_refs: dict[str, int] = {}
    skill_path_re = re.compile(r'/\.claude/skills/([^/]+)/')

    for d in _iter_lines(p):
        if d.get("type") != "assistant":
            continue
        content = (d.get("message") or {}).get("content", [])
        if not isinstance(content, list):
            continue
        for c in content:
            if not isinstance(c, dict) or c.get("type") != "tool_use":
                continue
            name = c.get("name", "")
            inp = c.get("input") or {}

            if name == "Skill":
                sk = inp.get("skill", "")
                if sk:
                    invokes[sk] = invokes.get(sk, 0) + 1

            elif name in ("Read", "Write", "Edit"):
                fp = str(inp.get("file_path", ""))
                m = skill_path_re.search(fp)
                if m:
                    sk = m.group(1)
                    file_ops[sk] = file_ops.get(sk, 0) + 1
                    if name == "Read":
                        skill_reads[sk] = skill_reads.get(sk, 0) + 1
                    else:
                        skill_writes[sk] = skill_writes.get(sk, 0) + 1

            elif name == "Bash":
                cmd = str(inp.get("command", ""))
                if "skills/" in cmd or "SKILL.md" in cmd:
                    matches = skill_path_re.findall(cmd)
                    if matches:
                        for sk in set(matches):
                            bash_refs[sk] = bash_refs.get(sk, 0) + 1
                    else:
                        bash_refs["_general"] = bash_refs.get("_general", 0) + 1

    ti = sum(invokes.values())
    tf = sum(file_ops.values())
    tr = sum(skill_reads.values())
    tw = sum(skill_writes.values())
    tb = sum(bash_refs.values())
    return {
        "per_skill_invokes": invokes,
        "per_skill_file_ops": file_ops,
        "per_skill_reads": skill_reads,
        "per_skill_writes": skill_writes,
        "per_skill_bash_refs": bash_refs,
        "totals": {"invoke": ti, "file_ops": tf, "reads": tr, "writes": tw, "bash_refs": tb, "total": ti + tf + tb},
    }


def count_memory_activity(path: str | Path) -> dict:
    """Count per-memory read/write/edit counts (not deduplicated)."""
    p = Path(path)
    if not p.exists():
        return {"per_memory_reads": {}, "per_memory_writes": {}, "per_memory_edits": {}}
    reads: dict[str, int] = {}
    writes: dict[str, int] = {}
    edits: dict[str, int] = {}
    for d in _iter_lines(p):
        if d.get("type") != "assistant":
            continue
        content = (d.get("message") or {}).get("content", [])
        if not isinstance(content, list):
            continue
        for c in content:
            if not isinstance(c, dict) or c.get("type") != "tool_use":
                continue
            tool_name = c.get("name", "")
            if tool_name not in ("Read", "Write", "Edit"):
                continue
            inp = c.get("input") or {}
            fp = str(inp.get("file_path", ""))
            if "/memory/" not in fp:
                continue
            mem_name = fp.rsplit("/", 1)[-1].replace(".md", "")
            if mem_name == "MEMORY":
                continue
            if tool_name == "Read":
                reads[mem_name] = reads.get(mem_name, 0) + 1
            elif tool_name == "Write":
                writes[mem_name] = writes.get(mem_name, 0) + 1
            elif tool_name == "Edit":
                edits[mem_name] = edits.get(mem_name, 0) + 1
    return {"per_memory_reads": reads, "per_memory_writes": writes, "per_memory_edits": edits}


def extract_memory_ops(path: str | Path) -> list[dict]:
    """Extract unique memory file operations: [{name, operation, content_preview?}]."""
    p = Path(path)
    if not p.exists():
        return []
    ops: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for d in _iter_lines(p):
        if d.get("type") != "assistant":
            continue
        content = (d.get("message") or {}).get("content", [])
        if not isinstance(content, list):
            continue
        for c in content:
            if not isinstance(c, dict) or c.get("type") != "tool_use":
                continue
            tool_name = c.get("name", "")
            if tool_name not in ("Read", "Write", "Edit"):
                continue
            inp = c.get("input") or {}
            file_path = str(inp.get("file_path", ""))
            if "/memory/" not in file_path:
                continue
            mem_name = file_path.rsplit("/", 1)[-1].replace(".md", "")
            if mem_name == "MEMORY":
                continue
            op = "read" if tool_name == "Read" else tool_name.lower()
            key = (mem_name, op)
            if key not in seen:
                seen.add(key)
                entry: dict = {"name": mem_name, "operation": op}
                if tool_name == "Write":
                    entry["content_preview"] = (inp.get("content") or "")[:300]
                elif tool_name == "Edit":
                    old = (inp.get("old_string") or "")[:100]
                    new = (inp.get("new_string") or "")[:100]
                    entry["content_preview"] = f"-{old}\n+{new}" if old else new[:200]
                ops.append(entry)
    return ops


# Heuristic: does a ScheduleWakeup reason/prompt describe waiting on GPU jobs
# (PACE / Slurm / specific GPU SKUs)? Used to tag the card "等 GPU".
_GPU_WAIT_RE = re.compile(
    r"\b(pace|gpu|slurm|squeue|sbatch|salloc|scancel|h100|h200|l40s?|a100|v100|cuda|vllm)\b"
    r"|rtx\s*\d{3,4}"
    r"|job\s*#?\d{5,}"
    r"|等\s*gpu",
    re.IGNORECASE,
)

# A wakeup this far past its scheduled time with no new activity means the
# harness never fired it — surface as stalled instead of working.
_WAKEUP_OVERDUE_GRACE_MS = 300_000

# ScheduleWakeup's runtime clamps delaySeconds to this range; mirror it so the
# predicted wake time matches what will actually happen.
_WAKEUP_MIN_DELAY_S = 60
_WAKEUP_MAX_DELAY_S = 3600


def _parse_ts_ms(ts: str) -> Optional[int]:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def _wakeup_info(block: dict, ts: str) -> Optional[dict]:
    inp = block.get("input") or {}
    try:
        delay = int(float(inp.get("delaySeconds", 0)))
    except (TypeError, ValueError):
        return None
    scheduled_ms = _parse_ts_ms(ts)
    if scheduled_ms is None or delay <= 0:
        return None
    delay = max(_WAKEUP_MIN_DELAY_S, min(_WAKEUP_MAX_DELAY_S, delay))
    reason = str(inp.get("reason") or "")
    prompt = str(inp.get("prompt") or "")
    wake_ms = scheduled_ms + delay * 1000
    now_ms = int(time.time() * 1000)
    is_gpu = bool(_GPU_WAIT_RE.search(reason + " " + prompt))
    return {
        "kind": "gpu" if is_gpu else "generic",
        "source": "wakeup",
        "reason": reason[:300],
        "prompt": prompt[:300],
        "delay_seconds": delay,
        "scheduled_at_ms": scheduled_ms,
        "wake_at_ms": wake_ms,
        "poll_interval_s": None,
        "overdue": now_ms > wake_ms + _WAKEUP_OVERDUE_GRACE_MS,
    }


def extract_pending_wakeup(path: str | Path) -> Optional[dict]:
    """Detect a ScheduleWakeup call that is still pending (session is sleeping).

    Walks the transcript tail backwards. The wakeup is pending iff the most
    recent assistant action is a ScheduleWakeup tool call and no real user
    input arrived after it (tool_result envelopes don't count — when the
    wakeup fires, the harness injects a user message, which clears this).
    """
    p = Path(path)
    if not p.exists():
        return None
    for d in reversed(_tail_lines(p, 80)):
        t = d.get("type")
        if t == "assistant":
            content = (d.get("message") or {}).get("content") or []
            if not isinstance(content, list):
                return None
            wakeup = None
            for c in content:
                if isinstance(c, dict) and c.get("type") == "tool_use" and c.get("name") == "ScheduleWakeup":
                    wakeup = c
            if wakeup is not None:
                return _wakeup_info(wakeup, d.get("timestamp") or "")
            # Thinking-only rows carry no action — keep walking. Any other
            # tool call or real text means the session moved past the wakeup.
            has_action = any(
                isinstance(c, dict) and (
                    c.get("type") == "tool_use"
                    or (c.get("type") == "text" and (c.get("text") or "").strip())
                )
                for c in content
            )
            if has_action:
                return None
        elif t == "user":
            content = (d.get("message") or {}).get("content") or []
            if isinstance(content, str):
                if content.strip():
                    return None
            elif isinstance(content, list):
                for c in content:
                    if not isinstance(c, dict):
                        continue
                    if c.get("type") == "text" and (c.get("text") or "").strip():
                        return None
                    # An errored tool_result means the wakeup call itself
                    # failed — the session is not actually sleeping.
                    if c.get("type") == "tool_result" and c.get("is_error"):
                        return None
        # system / summary / queue-operation rows: keep walking.
    return None


# Spawn acknowledgement of a background task. Modern Claude Code returns this
# as the IMMEDIATE tool_result of a run_in_background Bash / persistent
# Monitor; the real completion arrives later as a <task-notification>.
_BG_SPAWN_ACK_RE = re.compile(
    r"\b(?:running in background|will be notified|monitor(?:ing)? (?:started|running))\b",
    re.IGNORECASE,
)
# Task id inside a spawn ack. Two formats exist:
#   Bash bg:  "Command running in background with ID: b2g1awgbp."
#   Monitor:  "Monitor started (task bo5pebcx6, persistent — ...)"
_BG_TASK_ID_RE = re.compile(r"(?:\bID:\s*|\(task\s+)([a-zA-Z0-9_-]{4,})")
_TASK_NOTIF_TASK_ID_RE = re.compile(r"<task-id>([a-zA-Z0-9_-]+)</task-id>")
_TASK_NOTIF_TOOL_USE_RE = re.compile(r"<tool-use-id>([a-zA-Z0-9_-]+)</tool-use-id>")
_TASK_NOTIF_STATUS_RE = re.compile(r"<status>\s*(completed|failed|error|stopped)\s*</status>", re.IGNORECASE)
_SLEEP_RE = re.compile(r"\bsleep\s+(\d+)")


def _result_text(c: dict) -> str:
    cv = c.get("content")
    if isinstance(cv, str):
        return cv
    if isinstance(cv, list):
        return " ".join(x.get("text", "") for x in cv if isinstance(x, dict))
    return str(cv or "")


def extract_background_tasks(path: str | Path) -> list[dict]:
    """Extract ACTIVE (unresolved) background Bash/Monitor tasks.

    Lifecycle: the tool_use gets an immediate spawn-ack tool_result
    ("Command running in background with ID: <task_id> ... You will be
    notified when it completes"); completion arrives later as a
    <task-notification> user message referencing the tool_use id / task id.
    A task is active until such a notification, an explicit TaskStop, or a
    non-ack tool_result (legacy blocking completion / spawn error).
    """
    p = Path(path)
    if not p.exists():
        return []
    # Single pass collecting raw facts, resolution afterwards — transcript
    # rows are not strictly ordered (a tool_result can precede its tool_use).
    bg_uses: dict[str, dict] = {}       # tool_use_id -> task record
    stopped_tasks: set[str] = set()     # harness task ids killed via TaskStop
    ack_by_use: dict[str, str] = {}     # tool_use_id -> harness task id
    plain_result_ids: set[str] = set()  # tool_use_ids with a non-ack result
    notif_texts: list[str] = []

    for d in _iter_lines(p):
        t = d.get("type")
        if t == "assistant":
            for c in ((d.get("message") or {}).get("content") or []):
                if not isinstance(c, dict) or c.get("type") != "tool_use":
                    continue
                name = c.get("name", "")
                inp = c.get("input") or {}
                tid = c.get("id", "")
                if (name == "Bash" and inp.get("run_in_background") and tid) or \
                   (name == "Monitor" and inp.get("persistent") and tid):
                    cmd = str(inp.get("command") or "")
                    desc = str(inp.get("description") or "")
                    sleep_m = _SLEEP_RE.search(cmd)
                    bg_uses[tid] = {
                        "type": "bash_bg" if name == "Bash" else "monitor",
                        "description": desc[:200],
                        "command": cmd[:200],
                        "started_ts": d.get("timestamp") or "",
                        "task_id": None,
                        "poll_interval_s": int(sleep_m.group(1)) if sleep_m else None,
                        "is_gpu": bool(_GPU_WAIT_RE.search(cmd + " " + desc)),
                    }
                elif name == "TaskStop":
                    stop_id = str(inp.get("taskId") or inp.get("task_id") or "")
                    if stop_id:
                        stopped_tasks.add(stop_id)
        elif t == "user":
            content = (d.get("message") or {}).get("content") or []
            text_parts: list[str] = [content] if isinstance(content, str) else []
            for c in (content if isinstance(content, list) else []):
                if not isinstance(c, dict):
                    continue
                if c.get("type") == "text":
                    text_parts.append(c.get("text") or "")
                elif c.get("type") == "tool_result":
                    tu = c.get("tool_use_id", "")
                    if not tu:
                        continue
                    txt = _result_text(c)
                    id_m = _BG_TASK_ID_RE.search(txt)
                    if not c.get("is_error") and id_m and _BG_SPAWN_ACK_RE.search(txt):
                        ack_by_use.setdefault(tu, id_m.group(1))
                    else:
                        plain_result_ids.add(tu)
            full_text = " ".join(text_parts)
            if "<task-notification>" in full_text:
                notif_texts.append(full_text)
        elif t == "queue-operation":
            # Notifications queued while the session is busy land here first.
            blob = d.get("content")
            if isinstance(blob, str) and "<task-notification>" in blob:
                notif_texts.append(blob)
        elif t == "attachment":
            blob = (d.get("attachment") or {}).get("prompt")
            if isinstance(blob, str) and "<task-notification>" in blob:
                notif_texts.append(blob)

    # ---- resolution ----
    task_to_use = {tk: tu for tu, tk in ack_by_use.items() if tu in bg_uses}
    resolved: set[str] = set()
    for ft in notif_texts:
        # Persistent Monitor "event" pulses recur without ending the task.
        # Distinguish them STRUCTURALLY (pulses carry no <tool-use-id> and no
        # terminal <status>), never by summary text — the assistant-authored
        # task description is embedded verbatim and could say anything.
        is_event = not _TASK_NOTIF_TOOL_USE_RE.search(ft) and not _TASK_NOTIF_STATUS_RE.search(ft)
        candidates = list(_TASK_NOTIF_TOOL_USE_RE.findall(ft))
        candidates += [task_to_use[tk] for tk in _TASK_NOTIF_TASK_ID_RE.findall(ft) if tk in task_to_use]
        for tu in candidates:
            if tu in bg_uses and not (is_event and bg_uses[tu]["type"] == "monitor"):
                resolved.add(tu)

    out: list[dict] = []
    for tu, task in bg_uses.items():
        task["task_id"] = ack_by_use.get(tu)
        if tu in resolved:
            continue
        if task["task_id"] and task["task_id"] in stopped_tasks:
            continue
        # No ack and a plain result: spawn error or legacy blocking completion.
        if tu not in ack_by_use and tu in plain_result_ids:
            continue
        out.append(task)
    return out


def gpu_wait_from_background(tasks: list[dict]) -> Optional[dict]:
    """Synthesize a pending_wakeup-style record from an active GPU waiter.

    Background waiters have no scheduled wake time — wake_at_ms is None and
    poll_interval_s (parsed from `sleep N` in the command) hints the cadence.
    Picks the most recently started GPU waiter (several can coexist).
    """
    for t in reversed(tasks or []):
        if not t.get("is_gpu"):
            continue
        return {
            "kind": "gpu",
            "source": "background",
            "reason": (t.get("description") or t.get("command") or "")[:300],
            "prompt": "",
            "delay_seconds": None,
            "scheduled_at_ms": None,
            "wake_at_ms": None,
            "poll_interval_s": t.get("poll_interval_s"),
            "overdue": False,
        }
    return None


def extract_plan_history(path: str | Path) -> list[dict]:
    """Extract chronological plan file mutations from a transcript.

    Returns [{ts, plan_file, operation, version_label, content, diff}].
    Write = full content snapshot. Edit = old_string/new_string diff.
    """
    p = Path(path)
    if not p.exists():
        return []
    history: list[dict] = []
    write_count: dict[str, int] = {}
    edit_count: dict[str, int] = {}
    for d in _iter_lines(p):
        if d.get("type") != "assistant":
            continue
        ts = ""
        msg = d.get("message") or {}
        if "timestamp" not in msg and d.get("timestamp"):
            ts = d["timestamp"]
        else:
            ts = msg.get("timestamp", "")
        content_list = msg.get("content", [])
        if not isinstance(content_list, list):
            continue
        for c in content_list:
            if not isinstance(c, dict) or c.get("type") != "tool_use":
                continue
            tool_name = c.get("name", "")
            if tool_name not in ("Write", "Edit"):
                continue
            inp = c.get("input") or {}
            fp = str(inp.get("file_path", ""))
            if "/.claude/plans/" not in fp or not fp.endswith(".md"):
                continue
            plan_name = fp.rsplit("/", 1)[-1]
            if tool_name == "Write":
                write_count[plan_name] = write_count.get(plan_name, 0) + 1
                edit_count[plan_name] = 0
                vn = write_count[plan_name]
                history.append({
                    "ts": ts,
                    "plan_file": plan_name,
                    "operation": "write",
                    "version_label": f"v{vn}",
                    "content": inp.get("content", ""),
                    "diff": None,
                })
            elif tool_name == "Edit":
                vn = write_count.get(plan_name, 0)
                edit_count[plan_name] = edit_count.get(plan_name, 0) + 1
                en = edit_count[plan_name]
                old_s = inp.get("old_string", "")
                new_s = inp.get("new_string", "")
                history.append({
                    "ts": ts,
                    "plan_file": plan_name,
                    "operation": "edit",
                    "version_label": f"v{vn}.{en}",
                    "content": None,
                    "diff": {"old": old_s[:2000], "new": new_s[:2000]},
                })
    return history
