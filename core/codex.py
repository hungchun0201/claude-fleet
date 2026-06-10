"""Parse ~/.codex/sessions/ into HistorySession-compatible objects + timeline."""
from __future__ import annotations

import json
import re
import subprocess
import time
from dataclasses import dataclass, asdict, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from .sessions import HOME_BASE

CODEX_HOME = HOME_BASE / ".codex"
CODEX_SESSIONS_DIR = CODEX_HOME / "sessions"

# Match skill path like /.claude/skills/foo/ or /.codex/skills/foo/
# Stop at whitespace, quote, &&, ||, semicolons, or maxdepth/-flag args
_SKILL_PATH_RE = re.compile(r'/\.(?:claude|codex)/skills/([A-Za-z0-9_-]+)(?:/|\b)')
_MEMORY_PATH_RE = re.compile(r'/memory/([A-Za-z0-9_-]+)\.md')


@dataclass
class CodexSession:
    session_id: str
    project: str
    project_name: str
    first_input: str
    first_ts: str
    last_ts: str
    transcript_path: str
    transcript_size: int
    transcript_mtime: int
    cli_version: str
    model_provider: str
    model: str = ""
    skills_used: list = field(default_factory=list)
    memory_ops: list = field(default_factory=list)
    skill_breakdown: dict = field(default_factory=dict)

    def to_history_dict(self) -> dict:
        return {
            "session_id": self.session_id,
            "project": self.project,
            "project_name": self.project_name,
            "first_input": self.first_input,
            "input_count": 0,
            "first_ts": self.first_ts,
            "last_ts": self.last_ts,
            "transcript_path": self.transcript_path,
            "transcript_size": self.transcript_size,
            "transcript_mtime": self.transcript_mtime,
            "is_alive": False,
            "platform": "codex",
            "model": self.model,
            "skills_used": self.skills_used,
            "memory_ops": self.memory_ops,
            "skill_breakdown": self.skill_breakdown,
        }


def _parse_session_meta(path: Path) -> Optional[dict]:
    try:
        with path.open() as f:
            first_line = f.readline()
            d = json.loads(first_line)
            if d.get("type") != "session_meta":
                return None
            return d.get("payload") or {}
    except Exception:
        return None


def _extract_first_user_input(path: Path) -> str:
    """Try user input first, fall back to first assistant response text."""
    try:
        with path.open() as f:
            for line in f:
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                if d.get("type") == "event_msg":
                    payload = d.get("payload") or {}
                    if payload.get("role") == "user":
                        content = payload.get("content")
                        if isinstance(content, str) and content.strip():
                            return content[:300]
                        if isinstance(content, list):
                            for c in content:
                                if isinstance(c, dict) and c.get("type") == "input_text":
                                    t = (c.get("text") or "").strip()
                                    if t:
                                        return t[:300]
                if d.get("type") == "response_item":
                    payload = d.get("payload") or {}
                    if payload.get("type") == "message":
                        for c in (payload.get("content") or []):
                            if isinstance(c, dict) and c.get("type") == "output_text":
                                t = (c.get("text") or "").strip()
                                if t:
                                    return t[:300]
    except Exception:
        pass
    return ""


def extract_codex_session_activity(path: Path | str) -> dict:
    """Codex has no file I/O tools — everything goes through exec_command.
    We must scan the command strings for skill/memory file references.
    """
    p = Path(path)
    if not p.exists():
        return {
            "skills_used": [], "memory_ops": [], "model": "",
            "skill_breakdown": {
                "per_skill_invokes": {}, "per_skill_reads": {},
                "per_skill_writes": {}, "per_skill_bash_refs": {},
            },
        }

    bash_refs: dict[str, int] = {}
    skill_reads: dict[str, int] = {}
    skill_writes: dict[str, int] = {}
    memory_ops_seen: set[tuple[str, str]] = set()
    memory_ops: list[dict] = []
    model = ""

    try:
        with p.open() as f:
            for line in f:
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                t = d.get("type", "")
                payload = d.get("payload") or {}

                if t == "turn_context":
                    m = payload.get("model", "")
                    if m:
                        model = m

                if t != "response_item":
                    continue
                if payload.get("type") != "function_call":
                    continue
                name = payload.get("name", "")
                if name != "exec_command":
                    continue

                args_str = payload.get("arguments", "")
                try:
                    args = json.loads(args_str) if isinstance(args_str, str) else args_str
                except Exception:
                    args = {}
                cmd = str(args.get("cmd", "") or args.get("command", ""))
                workdir = str(args.get("workdir", ""))
                # Codex sets workdir to skill dir, then runs cmd inside it.
                # Need to scan both for skill references.
                haystack = cmd + " " + workdir
                if not haystack.strip():
                    continue

                # Skill path mentions (in cmd OR workdir)
                skill_matches = set(_SKILL_PATH_RE.findall(haystack))
                if skill_matches:
                    write_kw = any(k in cmd for k in ("write_file", " > ", " >> ", "tee ", "echo ", "cat <<", "cp ", "mv ", "mkdir"))
                    for sk in skill_matches:
                        bash_refs[sk] = bash_refs.get(sk, 0) + 1
                        if write_kw:
                            skill_writes[sk] = skill_writes.get(sk, 0) + 1
                        else:
                            skill_reads[sk] = skill_reads.get(sk, 0) + 1

                # Memory path mentions
                mem_matches = _MEMORY_PATH_RE.findall(haystack)
                for mem_name in set(mem_matches):
                    if mem_name == "MEMORY":
                        continue
                    write_kw = any(k in cmd for k in (" > ", " >> ", "tee ", "echo ", "cat <<"))
                    op = "write" if write_kw else "read"
                    key = (mem_name, op)
                    if key not in memory_ops_seen:
                        memory_ops_seen.add(key)
                        memory_ops.append({"name": mem_name, "operation": op})
    except Exception:
        pass

    skills_used = list(set(list(skill_reads.keys()) + list(skill_writes.keys())))
    return {
        "skills_used": skills_used,
        "memory_ops": memory_ops,
        "model": model,
        "skill_breakdown": {
            "per_skill_invokes": {},
            "per_skill_reads": skill_reads,
            "per_skill_writes": skill_writes,
            "per_skill_bash_refs": bash_refs,
        },
    }


def list_codex_sessions() -> list[CodexSession]:
    if not CODEX_SESSIONS_DIR.exists():
        return []
    sessions: list[CodexSession] = []
    for f in CODEX_SESSIONS_DIR.rglob("*.jsonl"):
        meta = _parse_session_meta(f)
        if not meta:
            continue
        try:
            st = f.stat()
        except Exception:
            continue
        cwd = meta.get("cwd", "")
        activity = extract_codex_session_activity(f)
        sessions.append(CodexSession(
            session_id=meta.get("id", f.stem),
            project=cwd,
            project_name=cwd.rsplit("/", 1)[-1] if cwd else f.stem,
            first_input=_extract_first_user_input(f),
            first_ts=meta.get("timestamp", ""),
            last_ts=meta.get("timestamp", ""),
            transcript_path=str(f),
            transcript_size=st.st_size,
            transcript_mtime=int(st.st_mtime * 1000),
            cli_version=meta.get("cli_version", ""),
            model_provider=meta.get("model_provider", ""),
            model=activity["model"],
            skills_used=activity["skills_used"],
            memory_ops=activity["memory_ops"],
            skill_breakdown=activity["skill_breakdown"],
        ))
    sessions.sort(key=lambda s: s.transcript_mtime, reverse=True)
    return sessions


# ---------- live Codex review detection ----------
#
# Two shapes exist in the wild:
#   exec: a Claude session shells out `codex exec "<prompt>"` (often piped to
#         tee). Detectable from the process tree; its rollout file STREAMS,
#         so we can show current activity and flag a stalled review.
#   mcp:  a Claude session calls the mcp__codex__codex tool. The in-flight
#         tool_use is not in the Claude transcript yet; the rollout is
#         written only at start (session_meta + task_started), so there is
#         no live progress — only the start time.

_EXEC_CMD_RE = re.compile(r"(?:^|[/\s])codex\s+exec\s")
EXEC_STALL_THRESHOLD_S = 15 * 60


def _parse_etime(s: str) -> int:
    """ps etime: [[dd-]hh:]mm:ss -> seconds."""
    s = s.strip()
    days = 0
    if "-" in s:
        d, s = s.split("-", 1)
        try:
            days = int(d)
        except ValueError:
            return 0
    parts = s.split(":")
    try:
        nums = [int(x) for x in parts]
    except ValueError:
        return 0
    if len(nums) == 3:
        h, m, sec = nums
    elif len(nums) == 2:
        h, (m, sec) = 0, nums
    else:
        return 0
    return days * 86400 + h * 3600 + m * 60 + sec


def _ps_snapshot() -> list[dict]:
    try:
        out = subprocess.check_output(
            ["ps", "-axo", "pid=,ppid=,etime=,command="],
            stderr=subprocess.DEVNULL, timeout=3,
        ).decode(errors="replace")
    except Exception:
        return []
    procs = []
    for line in out.splitlines():
        parts = line.split(None, 3)
        if len(parts) < 4:
            continue
        try:
            procs.append({
                "pid": int(parts[0]), "ppid": int(parts[1]),
                "etime_s": _parse_etime(parts[2]), "command": parts[3],
            })
        except ValueError:
            continue
    return procs


def find_exec_reviews(window_pids: list[int]) -> dict[int, dict]:
    """Map window pid -> {elapsed_s, pid, command} for descendant `codex exec`."""
    procs = _ps_snapshot()
    if not procs:
        return {}
    children: dict[int, list[dict]] = {}
    for pr in procs:
        children.setdefault(pr["ppid"], []).append(pr)

    out: dict[int, dict] = {}
    for wpid in window_pids:
        queue = list(children.get(wpid, []))
        seen: set[int] = set()
        while queue:
            pr = queue.pop(0)
            if pr["pid"] in seen:
                continue
            seen.add(pr["pid"])
            if _EXEC_CMD_RE.search(pr["command"]):
                out[wpid] = {
                    "elapsed_s": pr["etime_s"],
                    "pid": pr["pid"],
                    "command": pr["command"][:300],
                }
                break
            queue.extend(children.get(pr["pid"], []))
    return out


_ROLLOUT_META_CACHE: dict[str, dict] = {}


def recent_rollouts(max_age_s: int = 48 * 3600) -> list[dict]:
    """Codex rollout files written or started within max_age_s, newest first.

    Scans only today's and yesterday's date directories — this runs in the
    2s poll loop.
    """
    if not CODEX_SESSIONS_DIR.exists():
        return []
    now = time.time()
    dirs = []
    for delta in (0, 1):
        d = datetime.now() - timedelta(days=delta)
        dirs.append(CODEX_SESSIONS_DIR / f"{d.year:04d}" / f"{d.month:02d}" / f"{d.day:02d}")
    out: list[dict] = []
    for dd in dirs:
        if not dd.is_dir():
            continue
        for f in dd.glob("*.jsonl"):
            try:
                st = f.stat()
            except OSError:
                continue
            key = str(f)
            meta = _ROLLOUT_META_CACHE.get(key)
            if meta is None:
                meta = _parse_session_meta(f) or {}
                if meta:
                    _ROLLOUT_META_CACHE[key] = meta
            started_ms = None
            ts = meta.get("timestamp", "")
            if ts:
                try:
                    started_ms = int(datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp() * 1000)
                except ValueError:
                    pass
            fresh = (now - st.st_mtime) <= max_age_s or (
                started_ms is not None and (now * 1000 - started_ms) <= max_age_s * 1000
            )
            if not fresh:
                continue
            out.append({
                "path": key,
                "codex_session_id": meta.get("id", f.stem),
                "cwd": meta.get("cwd", ""),
                "originator": meta.get("originator", ""),
                "started_at_ms": started_ms,
                "last_write_ms": int(st.st_mtime * 1000),
                "size": st.st_size,
            })
    out.sort(key=lambda r: -(r["started_at_ms"] or 0))
    return out


def rollout_current_action(path: str | Path) -> Optional[str]:
    """Most recent meaningful event in a rollout, as a short human string."""
    from .transcripts import _tail_lines
    for d in reversed(_tail_lines(Path(path), 30)):
        payload = d.get("payload") or {}
        pt = payload.get("type", "")
        if d.get("type") == "response_item":
            if pt == "function_call":
                args = payload.get("arguments") or ""
                try:
                    cmd = json.loads(args).get("cmd") or json.loads(args).get("command") or ""
                except Exception:
                    cmd = ""
                return f"exec: {(cmd or args)[:100]}"
            if pt == "message":
                for c in (payload.get("content") or []):
                    if isinstance(c, dict) and c.get("type") == "output_text":
                        return f"output: {(c.get('text') or '')[:100]}"
            if pt == "reasoning":
                return "thinking…"
        elif d.get("type") == "event_msg" and pt == "task_started":
            return "task started"
    return None


def _cwd_related(a: str, b: str) -> bool:
    if not a or not b:
        return False
    return a == b or a.startswith(b + "/") or b.startswith(a + "/")


def detect_codex_review(window: dict, exec_map: dict[int, dict],
                        rollouts: list[dict], marker_ts_ms: Optional[int]) -> Optional[dict]:
    """Build the codex_review record for one window, or None."""
    now_ms = int(time.time() * 1000)
    pid = window.get("pid")
    cwd = window.get("cwd", "")

    ex = exec_map.get(pid)
    if ex:
        started_ms = now_ms - ex["elapsed_s"] * 1000
        # The exec-originated rollout whose start is closest after the
        # process start — NOT the newest: later review rounds in the same cwd
        # must not be attributed to a still-running (possibly hung) earlier
        # process. The backward slack only tolerates ps-etime rounding;
        # anything larger admits the PREVIOUS round's finished rollout, which
        # min() would then pick (wrong session id, false stalled flag).
        candidates = [
            r for r in rollouts
            if r["originator"] == "codex_exec" and _cwd_related(r["cwd"], cwd)
            and r["started_at_ms"] and r["started_at_ms"] >= started_ms - 30_000
        ]
        roll = min(candidates, key=lambda r: r["started_at_ms"], default=None)
        silent_s = (now_ms - roll["last_write_ms"]) // 1000 if roll else None
        return {
            "source": "exec",
            "elapsed_s": ex["elapsed_s"],
            "codex_session_id": roll["codex_session_id"] if roll else None,
            "streaming": roll is not None,
            "silent_s": silent_s,
            "stalled": silent_s is not None and silent_s > EXEC_STALL_THRESHOLD_S,
            "current_action": rollout_current_action(roll["path"]) if roll else None,
        }

    if marker_ts_ms is not None:
        roll = next(
            (r for r in rollouts
             if r["originator"] != "codex_exec" and r["cwd"] == cwd
             and r["started_at_ms"] and r["started_at_ms"] >= marker_ts_ms - 120_000),
            None,
        )
        started_ms = (roll["started_at_ms"] if roll else marker_ts_ms)
        return {
            "source": "mcp",
            "elapsed_s": max(0, (now_ms - started_ms) // 1000),
            "codex_session_id": roll["codex_session_id"] if roll else None,
            "streaming": False,
            "silent_s": None,
            "stalled": False,
            "current_action": None,
        }
    return None


def codex_timeline(path: str | Path, limit: int = 60) -> list[dict]:
    """Parse Codex JSONL into TurnEvent-compatible dicts."""
    p = Path(path)
    if not p.exists():
        return []
    events: list[dict] = []
    try:
        with p.open() as f:
            for line in f:
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                t = d.get("type")
                ts = d.get("timestamp", "")
                payload = d.get("payload") or {}

                if t == "event_msg":
                    role = payload.get("role", "")
                    content = payload.get("content")
                    text = ""
                    if isinstance(content, str):
                        text = content
                    elif isinstance(content, list):
                        for c in content:
                            if isinstance(c, dict) and c.get("type") == "input_text":
                                text = c.get("text", "")
                                break
                    if text and role == "user":
                        events.append({
                            "ts": ts, "kind": "user_text",
                            "text": text[:4000], "tool": None,
                            "role": "user", "extra": {},
                        })

                elif t == "response_item":
                    item_type = payload.get("type", "")
                    if item_type == "function_call":
                        events.append({
                            "ts": ts, "kind": "tool_use",
                            "text": "", "tool": payload.get("name", "function"),
                            "role": "assistant",
                            "extra": {"arguments": (payload.get("arguments") or "")[:200]},
                        })
                    elif item_type == "function_call_output":
                        events.append({
                            "ts": ts, "kind": "tool_result",
                            "text": (payload.get("output") or "")[:200],
                            "tool": None, "role": "user", "extra": {},
                        })
                    elif item_type == "message":
                        content = payload.get("content")
                        if isinstance(content, list):
                            for c in content:
                                if isinstance(c, dict) and c.get("type") == "output_text":
                                    events.append({
                                        "ts": ts, "kind": "assistant_text",
                                        "text": (c.get("text") or "")[:4000],
                                        "tool": None, "role": "assistant", "extra": {},
                                    })
    except Exception:
        pass
    return events[-limit:]
