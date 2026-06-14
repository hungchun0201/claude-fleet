"""Parse ~/.claude/projects/{slug}/{sessionId}.jsonl transcripts."""
from __future__ import annotations

import json
import os
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


# User messages also carry injected noise (tool results, slash-command output,
# system reminders, task notifications). A message that *starts* with one of
# these is not something the user typed.
_USER_NOISE_PREFIXES = (
    "<system-reminder", "<task-notification", "<local-command",
    "<command-name", "<command-message", "<command-args",
    "<user-prompt-submit-hook", "<bash-input", "<bash-stdout", "<bash-stderr",
    "Caveat:",
)


def _genuine_user_text(content) -> Optional[str]:
    """Clean typed text from a user-message `content`, or None if the message is
    noise (a tool result, slash-command output, or reminder/notification wrapper).
    Strips any appended <system-reminder> block from real input."""
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        if any(isinstance(c, dict) and c.get("type") == "tool_result" for c in content):
            return None
        text = " ".join(
            c.get("text") or "" for c in content
            if isinstance(c, dict) and c.get("type") == "text"
        )
    else:
        return None
    text = (text or "").strip()
    if not text or text.startswith(_USER_NOISE_PREFIXES):
        return None
    clean = re.sub(r"<system-reminder>.*?</system-reminder>", "", text, flags=re.DOTALL)
    clean = " ".join(clean.split())
    return clean or None


def last_user_input(path: str | Path) -> Optional[str]:
    """The user's most recent genuinely-typed prompt, as a one-line preview.

    Includes typeahead the user submitted while the assistant was still busy:
    that text lives in `queue-operation` enqueue rows (not a user turn yet), and
    is more recent than the last processed user message. Reads only the tail, so
    a prompt buried under a very long agentic turn may not be found.
    """
    p = Path(path)
    if not p.exists():
        return None
    for d in reversed(_tail_lines(p, 250)):
        typ = d.get("type")
        if typ == "user":
            text = _genuine_user_text((d.get("message") or {}).get("content"))
        elif typ == "queue-operation" and d.get("operation") == "enqueue":
            text = _genuine_user_text(d.get("content"))
        else:
            continue
        if text:
            return text[:200]
    return None


def first_user_input(path: str | Path) -> Optional[str]:
    """The session's first genuinely-typed prompt — a clean fallback title for
    unnamed sessions (skips the caveat/command wrappers that pollute the very
    first message)."""
    p = Path(path)
    if not p.exists():
        return None
    for i, d in enumerate(_iter_lines(p)):
        if i > 300:
            break
        if d.get("type") != "user":
            continue
        t = _genuine_user_text((d.get("message") or {}).get("content"))
        if t:
            return t[:120]
    return None


_TOTALS_CACHE: dict = {}


def session_token_totals(path: str | Path) -> dict:
    """Cumulative token usage for the whole session, grouped by model:
    {model: {input, cache_read, output, cache_write_5m, cache_write_1h}}.

    Reads the full transcript (cheap: a substring pre-filter skips the ~95% of
    rows with no usage block before json.loads), cached by (mtime, size) so it
    only recomputes when the transcript grows. Used for the per-card cost estimate.
    """
    p = Path(path)
    try:
        st = p.stat()
    except OSError:
        return {}
    key = str(p)
    cached = _TOTALS_CACHE.get(key)
    if cached and cached[0] == st.st_mtime and cached[1] == st.st_size:
        return cached[2]

    totals: dict = {}
    try:
        with p.open() as f:
            for line in f:
                if '"usage"' not in line:
                    continue
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                if d.get("type") != "assistant":
                    continue
                msg = d.get("message") or {}
                if not isinstance(msg, dict):
                    continue
                model = msg.get("model") or ""
                usage = msg.get("usage")
                if not isinstance(usage, dict) or model in ("", "<synthetic>"):
                    continue
                t = totals.setdefault(model, {
                    "input": 0, "cache_read": 0, "output": 0,
                    "cache_write_5m": 0, "cache_write_1h": 0,
                })
                t["input"] += int(usage.get("input_tokens") or 0)
                t["cache_read"] += int(usage.get("cache_read_input_tokens") or 0)
                t["output"] += int(usage.get("output_tokens") or 0)
                cc = usage.get("cache_creation") or {}
                m5 = int(cc.get("ephemeral_5m_input_tokens") or 0)
                m1 = int(cc.get("ephemeral_1h_input_tokens") or 0)
                if not (m5 or m1):  # no TTL split → assume the 5m default
                    m5 = int(usage.get("cache_creation_input_tokens") or 0)
                t["cache_write_5m"] += m5
                t["cache_write_1h"] += m1
    except OSError:
        return {}

    _TOTALS_CACHE[key] = (st.st_mtime, st.st_size, totals)
    return totals


def last_usage_and_model(path: str | Path) -> Optional[dict]:
    """Latest model + token usage for a session card.

    Reads only the transcript tail. Returns the most recent *real* assistant
    turn's model and a token breakdown:
      - context_tokens: input + cache_read + cache_creation of that turn — how
        full the context window currently is ("目前使用的 token 數量").
      - out_tokens: output tokens of that turn.
    Synthetic rows (model "<synthetic>", e.g. API-error notices) carry no real
    model or usage and are skipped.
    """
    p = Path(path)
    if not p.exists():
        return None
    for d in reversed(_tail_lines(p, 60)):
        if d.get("type") != "assistant":
            continue
        msg = d.get("message") or {}
        if not isinstance(msg, dict):
            continue
        model = msg.get("model") or ""
        usage = msg.get("usage")
        if not model or model == "<synthetic>" or not isinstance(usage, dict):
            continue
        ctx = (
            int(usage.get("input_tokens") or 0)
            + int(usage.get("cache_read_input_tokens") or 0)
            + int(usage.get("cache_creation_input_tokens") or 0)
        )
        return {
            "model": model,
            "context_tokens": ctx,
            "out_tokens": int(usage.get("output_tokens") or 0),
        }
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


# Heuristic: does this text describe waiting on GPU jobs (Slurm tooling /
# specific GPU SKUs)? Used to tag the card "等 GPU". Custom boundaries treat
# '-' and '/' as word characters so identifiers like a branch named
# feat/gpu-wait-tag or a file called h100-jitter.json never match — only
# free-standing words ("squeue", "L40S jobs", "等 GPU") do.
# Deliberately NOT in the list: the bare hostname "pace" — every remote chore
# (du scans, quota checks, rsync) runs over `ssh pace`, and tagging those
# 等GPU is wrong. A real GPU wait always carries a Slurm/GPU token.
_GPU_WAIT_RE = re.compile(
    r"(?<![\w/-])(?:gpu|slurm|squeue|sbatch|salloc|scancel|sacct|h100|h200|l40s?|a100|v100|cuda|vllm)(?![\w/-])"
    r"|(?<![\w/-])rtx\s*\d{3,4}(?![\w/-])"
    r"|\bjob\s*#?\d{5,}"
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
    # Classify from the REASON only — the prompt is a /loop continuation
    # payload that can mention branch/file names ("feat/gpu-wait-tag") that
    # have nothing to do with what the session is waiting for.
    is_gpu = bool(_GPU_WAIT_RE.search(reason))
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


def codex_call_marker(path: str | Path) -> Optional[int]:
    """Detect an in-flight Codex MCP call at the transcript tail.

    Returns the marker timestamp (ms) or None. Two shapes:
    - an mcp__codex__* tool_use with no tool_result yet, or
    - the most recent assistant action is ToolSearch loading the codex tool
      (the actual MCP call is in flight and not yet written to the
      transcript — observed in real sessions).
    """
    p = Path(path)
    if not p.exists():
        return None
    seen_results: set[str] = set()
    for d in reversed(_tail_lines(p, 60)):
        t = d.get("type")
        if t == "user":
            content = (d.get("message") or {}).get("content") or []
            if isinstance(content, str):
                if content.strip():
                    return None
                continue
            if not isinstance(content, list):
                continue
            for c in content:
                if not isinstance(c, dict):
                    continue
                if c.get("type") == "text" and (c.get("text") or "").strip():
                    return None
                if c.get("type") == "tool_result":
                    seen_results.add(c.get("tool_use_id", ""))
        elif t == "assistant":
            content = (d.get("message") or {}).get("content") or []
            if not isinstance(content, list):
                return None
            has_action = False
            for c in content:
                if not isinstance(c, dict):
                    continue
                ct = c.get("type")
                if ct == "tool_use":
                    has_action = True
                    name = c.get("name", "")
                    if name.startswith("mcp__codex__") and c.get("id", "") not in seen_results:
                        return _parse_ts_ms(d.get("timestamp") or "")
                    if name == "ToolSearch" and "codex" in str((c.get("input") or {}).get("query", "")).lower():
                        return _parse_ts_ms(d.get("timestamp") or "")
                elif ct == "text" and (c.get("text") or "").strip():
                    has_action = True
            if has_action:
                # Most recent assistant action is something else.
                return None
            # thinking-only row — keep walking
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
# A background task that runs a Codex review is not a GPU waiter, even if its
# prompt text mentions GPUs (e.g. reviewing a GPU-memory article).
_CODEX_TASK_RE = re.compile(r"\bcodex\s+(?:exec|mcp)\b|codex\s+review", re.IGNORECASE)
# Pull the ssh target and Slurm job ids out of a waiter command so the
# dashboard can poll the queue itself and show the latest job states.
_SSH_HOST_RE = re.compile(r"\bssh\s+((?:-[A-Za-z]\s+\S+\s+|--?[\w-]+(?:=\S+)?\s+)*)([A-Za-z0-9_.@-]+)")
_JOB_IDS_RE = re.compile(r"-j\s*([0-9][0-9,]*)")
_BG_OUTPUT_FILE_RE = re.compile(r"Output is being written to:\s*(\S+)")
# Workflow runs share the bg-task lifecycle: immediate spawn ack ("Workflow
# launched in background. Task ID: ... You will be notified when it
# completes"), then a <task-notification> on completion. The ack also carries
# the run's transcript dir, whose journal.jsonl gives live agent progress.
_WF_ACK_DIR_RE = re.compile(r"Transcript dir:\s*(\S+)")
_WF_ACK_SUMMARY_RE = re.compile(r"Summary:\s*([^\n]+)")
_WF_ACK_RUN_ID_RE = re.compile(r"Run ID:\s*(wf_[a-z0-9-]+)")
# meta.name from an inline script. `export const meta = {...}` is required to
# be the first statement, so the first name: in the text is meta's.
_WF_META_NAME_RE = re.compile(r"\bname:\s*['\"]([^'\"\n]{1,80})['\"]")
# No journal/agent-transcript writes for this long → presumed hung
# (mirrors codex.EXEC_STALL_THRESHOLD_S).
_WORKFLOW_STALL_THRESHOLD_S = 15 * 60


def _result_text(c: dict) -> str:
    cv = c.get("content")
    if isinstance(cv, str):
        return cv
    if isinstance(cv, list):
        return " ".join(x.get("text", "") for x in cv if isinstance(x, dict))
    return str(cv or "")


def _workflow_name_from_input(inp: dict) -> str:
    """Best-effort workflow name: explicit name > script meta > scriptPath stem."""
    name = str(inp.get("name") or "")
    if name:
        return name[:80]
    m = _WF_META_NAME_RE.search(str(inp.get("script") or ""))
    if m:
        return m.group(1)
    sp = str(inp.get("scriptPath") or "")
    if sp:
        stem = sp.rsplit("/", 1)[-1].removesuffix(".js")
        # Persisted scripts are named <meta-name>-<run-id>.js
        return re.sub(r"-wf_[a-z0-9-]+$", "", stem)[:80]
    return ""


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
                if name == "Workflow" and tid:
                    # Workflows always run in background; same ack/notification
                    # lifecycle as bg Bash. Description comes from the ack's
                    # Summary line during resolution.
                    bg_uses[tid] = {
                        "type": "workflow",
                        "description": "",
                        "command": "",
                        "started_ts": d.get("timestamp") or "",
                        "task_id": None,
                        "output_file": None,
                        "poll_interval_s": None,
                        "ssh_host": None,
                        "job_ids": [],
                        "is_gpu": False,
                        "workflow_name": _workflow_name_from_input(inp),
                        "workflow_dir": None,
                        "run_id": None,
                    }
                elif (name == "Bash" and inp.get("run_in_background") and tid) or \
                   (name == "Monitor" and inp.get("persistent") and tid):
                    cmd = str(inp.get("command") or "")
                    desc = str(inp.get("description") or "")
                    sleep_m = _SLEEP_RE.search(cmd)
                    host_m = _SSH_HOST_RE.search(cmd)
                    ids_m = _JOB_IDS_RE.search(cmd)
                    haystack = cmd + " " + desc
                    bg_uses[tid] = {
                        "type": "bash_bg" if name == "Bash" else "monitor",
                        "description": desc[:200],
                        "command": cmd[:200],
                        "started_ts": d.get("timestamp") or "",
                        "task_id": None,
                        "output_file": None,
                        "poll_interval_s": int(sleep_m.group(1)) if sleep_m else None,
                        "ssh_host": host_m.group(2) if host_m else None,
                        "job_ids": ids_m.group(1).split(",") if ids_m else [],
                        "is_gpu": bool(_GPU_WAIT_RE.search(haystack)) and not _CODEX_TASK_RE.search(haystack),
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
                        out_m = _BG_OUTPUT_FILE_RE.search(txt)
                        dir_m = _WF_ACK_DIR_RE.search(txt)
                        sum_m = _WF_ACK_SUMMARY_RE.search(txt)
                        run_m = _WF_ACK_RUN_ID_RE.search(txt)
                        ack_by_use.setdefault(tu, {
                            "task_id": id_m.group(1),
                            "output_file": out_m.group(1).rstrip(".") if out_m else None,
                            "workflow_dir": dir_m.group(1) if dir_m else None,
                            "summary": sum_m.group(1).strip() if sum_m else None,
                            "run_id": run_m.group(1) if run_m else None,
                        })
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
    task_to_use = {a["task_id"]: tu for tu, a in ack_by_use.items() if tu in bg_uses}
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
        ack = ack_by_use.get(tu) or {}
        task["task_id"] = ack.get("task_id")
        task["output_file"] = ack.get("output_file")
        if task["type"] == "workflow":
            task["workflow_dir"] = ack.get("workflow_dir")
            task["run_id"] = ack.get("run_id")
            if ack.get("summary"):
                task["description"] = str(ack["summary"])[:200]
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
            "ssh_host": t.get("ssh_host"),
            "job_ids": t.get("job_ids") or [],
            "output_file": t.get("output_file"),
            "overdue": False,
        }
    return None


def _workflow_dir_progress(d: Path) -> Optional[dict]:
    """Agent counts from journal.jsonl + freshness from file mtimes.

    The run dir holds journal.jsonl plus one agent-<id>.jsonl per spawned
    agent; live agents append constantly, so the newest mtime across the dir
    is a good "still alive" signal.
    """
    try:
        entries = list(os.scandir(d))
    except OSError:
        return None
    last_mtime = 0.0
    for e in entries:
        try:
            last_mtime = max(last_mtime, e.stat().st_mtime)
        except OSError:
            continue
    started = done = 0
    for row in _iter_lines(d / "journal.jsonl"):
        rt = row.get("type")
        if rt == "started":
            started += 1
        elif rt in ("result", "error"):
            done += 1
    return {
        "agents_started": started,
        "agents_done": done,
        "silent_s": max(0, int(time.time() - last_mtime)) if last_mtime else None,
    }


def active_workflow_run(tasks: list[dict]) -> Optional[dict]:
    """Most recent active Workflow run, enriched with live journal progress.

    A workflow runs in the background while the main turn usually ends —
    without this signal the session would triage as "completed" even with
    dozens of agents still working. Stalled = no file writes in the run dir
    for _WORKFLOW_STALL_THRESHOLD_S.
    """
    for t in reversed(tasks or []):
        if t.get("type") != "workflow":
            continue
        now_ms = int(time.time() * 1000)
        started_ms = _parse_ts_ms(t.get("started_ts") or "")
        run = {
            "name": t.get("workflow_name") or "workflow",
            "description": (t.get("description") or "")[:200],
            "task_id": t.get("task_id"),
            "run_id": t.get("run_id"),
            "started_at_ms": started_ms,
            "elapsed_s": max(0, (now_ms - started_ms) // 1000) if started_ms else None,
            "agents_started": None,
            "agents_done": None,
            "silent_s": None,
            "stalled": False,
        }
        wf_dir = t.get("workflow_dir")
        if wf_dir:
            prog = _workflow_dir_progress(Path(wf_dir))
            if prog:
                run.update(prog)
                silent = run.get("silent_s")
                run["stalled"] = silent is not None and silent > _WORKFLOW_STALL_THRESHOLD_S
        return run
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
