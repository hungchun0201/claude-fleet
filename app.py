"""Claude Fleet — FastAPI app: dashboard backend + SSE."""
from __future__ import annotations

import asyncio
import json
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sse_starlette.sse import EventSourceResponse

from core import actions, alerts, codex, history, memory, patrol, perms, plan_usage, plans, remote, search, sessions, shells, skills, transcripts, usage, vscode

HERE = Path(__file__).parent
STATIC_DIR = HERE / "static"


# ---------- shared in-memory state ----------

class State:
    def __init__(self) -> None:
        self.last_snapshot: dict = {"windows": [], "counts": {}, "ts": 0}
        self.subscribers: set[asyncio.Queue] = set()


state = State()


# ---------- remote GPU queue polling ----------
#
# GPU waiters often run quiet loops (grep -q), so their output files show
# nothing between checks. The dashboard polls the queue itself: every
# GPU_POLL_INTERVAL_S it runs sacct+squeue over ssh for the job ids parsed
# from the waiter command, and the snapshot attaches the latest result.

GPU_POLL_INTERVAL_S = 180
GPU_POLL_SSH_TIMEOUT_S = 25

_gpu_poll_cache: dict[tuple, dict] = {}
_gpu_poll_inflight: set[tuple] = set()


def _gpu_poll_targets() -> set[tuple]:
    targets: set[tuple] = set()
    for w in state.last_snapshot.get("windows", []):
        pw = w.get("pending_wakeup") or {}
        host, ids = pw.get("ssh_host"), pw.get("job_ids")
        if host and ids and all(i.isdigit() for i in ids):
            targets.add((host, tuple(ids)))
    return targets


async def _poll_one(host: str, ids: tuple) -> None:
    key = (host, ids)
    _gpu_poll_inflight.add(key)
    try:
        idstr = ",".join(ids)
        remote = (
            f"sacct -j {idstr} -X -n -o JobID%-9,JobName%-24,State%-10,Elapsed%-11 2>/dev/null; "
            f"squeue -j {idstr} -h -o '%i %T est-start:%S' -t PD 2>/dev/null"
        )
        proc = await asyncio.create_subprocess_exec(
            "ssh", "-o", "ConnectTimeout=15", "-o", "BatchMode=yes", host, remote,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=GPU_POLL_SSH_TIMEOUT_S)
        text = out.decode(errors="replace").strip()
        if proc.returncode == 0 and text:
            _gpu_poll_cache[key] = {"ts_ms": int(time.time() * 1000), "text": text[:1500], "ok": True}
        elif key not in _gpu_poll_cache:
            _gpu_poll_cache[key] = {"ts_ms": int(time.time() * 1000), "text": "", "ok": False}
    except Exception:
        if key not in _gpu_poll_cache:
            _gpu_poll_cache[key] = {"ts_ms": int(time.time() * 1000), "text": "", "ok": False}
    finally:
        _gpu_poll_inflight.discard(key)


async def _gpu_poller() -> None:
    while True:
        try:
            now_ms = int(time.time() * 1000)
            for key in _gpu_poll_targets():
                cached = _gpu_poll_cache.get(key)
                fresh = cached and (now_ms - cached["ts_ms"]) < GPU_POLL_INTERVAL_S * 1000
                if not fresh and key not in _gpu_poll_inflight:
                    asyncio.create_task(_poll_one(*key))
        except Exception as e:
            print(f"[gpu-poller] error: {e}")
        await asyncio.sleep(5)


# ---------- plan usage polling ----------
#
# The real plan limits come from an OAuth-gated endpoint (keychain read + HTTPS
# fetch). That is too slow for the 2-second snapshot loop, so we refresh it on a
# slow background poller; the snapshot only ever reads the cached value.

PLAN_USAGE_POLL_INTERVAL_S = 60


async def _plan_usage_poller() -> None:
    while True:
        try:
            await asyncio.to_thread(plan_usage.refresh)
        except Exception as e:
            print(f"[plan-usage-poller] error: {e}")
        await asyncio.sleep(PLAN_USAGE_POLL_INTERVAL_S)


# ---------- remote (lab) session polling ----------
#
# claude-lab sessions live on a remote box; SSH-polling their session files is
# too slow for the 2s loop, so a background poller fetches them and the snapshot
# merges the cached result. If the host goes unreachable the last-known sessions
# stay (marked stale) until a poll succeeds again — the tmux survives the drop.

LAB_POLL_INTERVAL_S = 8
LAB_STALE_AFTER_S = 25

_lab_cache: dict = {"ts": 0.0, "windows": []}


async def _lab_poller() -> None:
    while True:
        try:
            wins = await asyncio.to_thread(remote.poll)
            # Only overwrite on a real result; a transient SSH failure ([]) keeps
            # the last-known sessions (they're staying alive in tmux regardless).
            if wins:
                _lab_cache["windows"] = wins
                _lab_cache["ts"] = time.time()
            elif _lab_cache["windows"] and (time.time() - _lab_cache["ts"]) > 600:
                _lab_cache["windows"] = []  # gone for 10 min → assume host down/rebooted
        except Exception as e:
            print(f"[lab-poller] error: {e}")
        await asyncio.sleep(LAB_POLL_INTERVAL_S)


def _attach_last_poll(pw: dict) -> None:
    """Decorate a GPU-wait record with the latest queue state we know."""
    host, ids = pw.get("ssh_host"), pw.get("job_ids")
    now_ms = int(time.time() * 1000)
    if host and ids:
        cached = _gpu_poll_cache.get((host, tuple(ids)))
        if cached and cached.get("ok"):
            pw["last_poll"] = {
                "text": cached["text"],
                "ago_s": max(0, (now_ms - cached["ts_ms"]) // 1000),
                "source": "dashboard",
            }
            return
    # Fallback: tail of the waiter's own output file (chatty waiters).
    of = pw.get("output_file")
    if of:
        try:
            p = Path(of)
            st = p.stat()
            if st.st_size > 0:
                with p.open("rb") as f:
                    f.seek(max(0, st.st_size - 1500))
                    tail = f.read().decode(errors="replace").strip()
                pw["last_poll"] = {
                    "text": tail[-1200:],
                    "ago_s": max(0, int(time.time() - st.st_mtime)),
                    "source": "waiter",
                }
                return
        except OSError:
            pass
    pw["last_poll"] = None


def _ui_version() -> str:
    """Frontend build stamp; the client hard-reloads when it changes."""
    try:
        return str(int((STATIC_DIR / "index.html").stat().st_mtime))
    except OSError:
        return "0"


def _remote_status_triage(w: dict) -> dict:
    """Triage a remote session with no transcript yet — status/idle only."""
    idle = w.get("idle_seconds", 0)
    if w.get("status") == "busy" and idle < patrol.IDLE_THRESHOLD:
        return {"triage": "working", "reason": "工作中", "suggestion": ""}
    if idle >= patrol.CLOSEABLE_THRESHOLD:
        return {"triage": "closeable", "reason": f"閒置 {idle // 60}m", "suggestion": "可關閉"}
    return {"triage": "completed", "reason": f"閒置 {idle // 60}m", "suggestion": ""}


def _enrich_remote(rw: dict, vs_info: dict | None, stale: bool) -> dict:
    """Turn a cached remote (lab) session into a full window dict for the card."""
    w = dict(rw)
    tp = w.get("transcript_path")
    # Is a local laptop terminal attached to this tmux right now? If so it's
    # focusable (reuse the VS Code focus) and the active-ring can mark it.
    att = remote.local_attachment_pid(w.get("name"), vs_info) if vs_info else None
    w["attached"] = att is not None
    w["attached_pid"] = att
    d = vscode.detect(att, vs_info) if att and vs_info else None
    w["shell_pid"] = d["shell_pid"] if d else None
    w["stale"] = stale
    if tp:
        w["current_task"] = transcripts.current_task_hint(tp)
        w["last_user_input"] = transcripts.last_user_input(tp)
        w["usage"] = transcripts.last_usage_and_model(tp)
        w["skills_used"] = transcripts.extract_skills_used(tp)
        w["memory_ops"] = transcripts.extract_memory_ops(tp)
    else:
        w.update(current_task=None, last_user_input=None, usage=None, skills_used=[], memory_ops=[])
    # Fields the local enrichment sets that don't apply to a remote session.
    w.update(permission_msg=None, permission_ts=None, background_tasks=[],
             workflow_run=None, pending_wakeup=None, codex_review=None, first_input=None)
    tri = patrol.classify(w) if tp else _remote_status_triage(w)
    w["triage"], w["triage_reason"], w["triage_suggestion"] = tri["triage"], tri["reason"], tri["suggestion"]
    return w


def _enriched_snapshot() -> dict:
    snap = sessions.snapshot()
    snap["ui_version"] = _ui_version()
    perm_by_tty = perms.pending_by_tty()
    exec_reviews = codex.find_exec_reviews([w["pid"] for w in snap["windows"]])
    rollouts = codex.recent_rollouts()
    # One process snapshot per tick, only when some session has a lingering
    # shell — used to show what each 🐚 background shell is actually running.
    shell_rows = shells._ps_rows() if any(w["status"] == "shell" for w in snap["windows"]) else None
    # Per-window terminal shell pid. The active-terminal ring is matched against
    # this on the CLIENT (which polls /api/vscode-active at ~250ms) so the
    # highlight is near-instant instead of waiting for the 2s snapshot.
    vs_info = vscode._ps_parents() if snap["windows"] else None
    for w in snap["windows"]:
        tty = w.get("tty")
        if tty and tty in perm_by_tty:
            ev = perm_by_tty[tty]
            w["permission_msg"] = ev.msg
            w["permission_ts"] = ev.raw_ts
        else:
            w["permission_msg"] = None
            w["permission_ts"] = None
        tp = w.get("transcript_path")
        if not w.get("name") and tp:
            first = transcripts.first_user_input(tp)
            if first:
                w["first_input"] = first
        if tp:
            w["current_task"] = transcripts.current_task_hint(tp)
            w["background_tasks"] = transcripts.extract_background_tasks(tp)
            w["workflow_run"] = transcripts.active_workflow_run(w["background_tasks"])
            # Sleeping on a ScheduleWakeup wins (it has a concrete wake time);
            # otherwise an active GPU background waiter also counts as waiting.
            w["pending_wakeup"] = (
                transcripts.extract_pending_wakeup(tp)
                or transcripts.gpu_wait_from_background(w["background_tasks"])
            )
            if w["pending_wakeup"]:
                _attach_last_poll(w["pending_wakeup"])
        else:
            w["current_task"] = None
            w["background_tasks"] = []
            w["workflow_run"] = None
            w["pending_wakeup"] = None
        # In-flight MCP codex calls leave no transcript row; look for the
        # marker on every busy window — a lingering (possibly hung) exec
        # child must not mask a concurrent hung MCP call on the same window.
        marker = None
        if tp and w["status"] == "busy":
            marker = transcripts.codex_call_marker(tp)
        w["codex_review"] = codex.detect_codex_review(w, exec_reviews, rollouts, marker)
        tri = patrol.classify(w)
        w["triage"] = tri["triage"]
        w["triage_reason"] = tri["reason"]
        w["triage_suggestion"] = tri["suggestion"]
        if tp:
            w["skills_used"] = transcripts.extract_skills_used(tp)
            w["memory_ops"] = transcripts.extract_memory_ops(tp)
            w["usage"] = transcripts.last_usage_and_model(tp)
            w["last_user_input"] = transcripts.last_user_input(tp)
        else:
            w["skills_used"] = []
            w["memory_ops"] = []
            w["usage"] = None
            w["last_user_input"] = None
        # What the lingering background shell(s) are running (turn-done sessions).
        w["shells"] = (
            shells.background_shells(w["pid"], rows=shell_rows)
            if w["status"] == "shell" and shell_rows is not None else []
        )
        # Terminal shell pid (== vscode.window.activeTerminal.processId when this
        # is the active one); the client rings the matching card.
        w["shell_pid"] = (vscode.detect(w["pid"], vs_info) or {}).get("shell_pid") if vs_info else None
    # Merge remote (lab) sessions, enriched the same way. Reuse the process tree
    # already built for local windows (it also contains the claude-lab procs).
    if _lab_cache["windows"]:
        if vs_info is None:
            vs_info = vscode._ps_parents()
        stale = (time.time() - _lab_cache["ts"]) > LAB_STALE_AFTER_S
        for rw in _lab_cache["windows"]:
            snap["windows"].append(_enrich_remote(rw, vs_info, stale))

    # Sort by triage priority (most urgent first), then by idle time.
    snap["windows"].sort(key=lambda w: (
        patrol.TRIAGE_PRIORITY.get(w.get("triage", ""), 99),
        -w.get("updated_at", 0),
    ))
    snap["usage_summary"] = usage.summary()
    snap["plan_usage"] = plan_usage.cached()  # real limits; refreshed off-path
    return snap


async def _watcher() -> None:
    """Poll sessions every 2s; broadcast each tick to SSE subscribers.

    Every tick is pushed unconditionally (aris-monitor style) so idle times,
    task hints, and time-driven triage flips stay live in the browser. The
    frontend patches DOM in place (Alpine keyed by pid) — no page reload.
    """
    while True:
        try:
            snap = _enriched_snapshot()
            state.last_snapshot = snap
            for alert in alerts.check(snap):
                asyncio.create_task(asyncio.to_thread(alerts.push, alert))
            payload = json.dumps(snap)
            dead: list[asyncio.Queue] = []
            for q in list(state.subscribers):
                try:
                    q.put_nowait(payload)
                except asyncio.QueueFull:
                    dead.append(q)
            for q in dead:
                state.subscribers.discard(q)
        except Exception as e:
            print(f"[watcher] error: {e}")
        await asyncio.sleep(2)


@asynccontextmanager
async def lifespan(app: FastAPI):
    tasks = [
        asyncio.create_task(_watcher()),
        asyncio.create_task(_gpu_poller()),
        asyncio.create_task(_plan_usage_poller()),
        asyncio.create_task(_lab_poller()),
    ]
    try:
        yield
    finally:
        for t in tasks:
            t.cancel()


app = FastAPI(title="Claude Fleet", lifespan=lifespan)


# ---------- routes ----------

@app.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    html = (STATIC_DIR / "index.html").read_text()
    # Never let the browser cache the shell — a stale frontend renders new
    # payload fields wrongly (and can't see the ui_version reload signal).
    return HTMLResponse(html, headers={"Cache-Control": "no-store"})


@app.get("/api/windows")
def api_windows() -> dict:
    if not state.last_snapshot["windows"]:
        state.last_snapshot = _enriched_snapshot()
    return state.last_snapshot


@app.get("/api/windows/{pid}/timeline")
def api_timeline(pid: int, limit: int = 2000) -> dict:
    w = sessions.find_window(pid)
    if not w:
        raise HTTPException(404, "window not found")
    tp = w.transcript_path or ""
    events = transcripts.timeline(tp, limit=limit) if tp else []
    return {
        "pid": pid,
        "session_id": w.session_id,
        "project_name": w.project_name,
        "events": events,
        "skills_used": transcripts.extract_skills_used(tp) if tp else [],
        "memory_ops": transcripts.extract_memory_ops(tp) if tp else [],
        "plan_history": transcripts.extract_plan_history(tp) if tp else [],
    }


@app.get("/api/windows/{pid}/plan")
def api_plan(pid: int) -> dict:
    w = sessions.find_window(pid)
    if not w:
        raise HTTPException(404, "window not found")
    plan = plans.plan_for_session(w.name, w.cwd, w.transcript_path)
    return {"pid": pid, "plan": plan}


@app.get("/api/search")
def api_search(q: str, limit: int = 60) -> dict:
    if not q.strip():
        return {"hits": [], "q": q}
    return {"hits": search.search(q, limit=limit), "q": q}


@app.get("/api/plans")
def api_plans() -> dict:
    return {"plans": plans.list_plans()}


@app.get("/api/plans/{name}")
def api_plan_by_name(name: str) -> dict:
    p = plans.read_plan_by_name(name)
    if not p:
        raise HTTPException(404, "plan not found")
    return p


@app.post("/api/windows/{pid}/focus")
def api_focus(pid: int) -> dict:
    # Remote (lab) session: focus the laptop terminal attached to its tmux. If
    # nothing is attached, the tmux still lives on the host — tell the user to
    # reattach with claude-lab.
    for rw in _lab_cache["windows"]:
        if rw.get("pid") == pid:
            att = remote.local_attachment_pid(rw.get("name"), vscode._ps_parents())
            if not att:
                suffix = (rw.get("name") or "").removeprefix("lab-")
                return {"ok": False, "detached": True,
                        "error": f"沒有本機終端附著（tmux 仍在 {rw.get('host')} 上）— 在 VSCode 執行 `claude-lab {suffix}` 重新附著"}
            return vscode.focus(att) or {"ok": False, "error": "attached terminal is not a VS Code terminal"}

    w = sessions.find_window(pid)
    if not w:
        raise HTTPException(404, "window not found")
    # Sessions inside a VS Code-family integrated terminal can't be raised by the
    # Terminal.app/iTerm2 AppleScript — route them to the companion extension.
    via_vscode = vscode.focus(pid)
    if via_vscode is not None:
        return via_vscode
    if not w.tty:
        return {"ok": False, "error": "no tty available for this pid"}
    return actions.focus_terminal(w.tty)


@app.post("/api/windows/{pid}/fork")
def api_fork(pid: int) -> dict:
    return actions.fork_session(pid)


@app.post("/api/windows/{pid}/export")
def api_export(pid: int) -> dict:
    return actions.export_to_feishu(pid)


@app.post("/api/windows/{pid}/close")
def api_close(pid: int) -> dict:
    return actions.close_session(pid)


@app.post("/api/windows/{pid}/review")
def api_review(pid: int) -> dict:
    return actions.review_session_start(pid)


@app.get("/api/windows/{pid}/review")
def api_review_result(pid: int) -> dict:
    return actions.review_session_result(pid)


@app.get("/api/history")
def api_history(q: str = "", page: int = 1, limit: int = 30) -> dict:
    return history.list_sessions(q=q or None, page=page, limit=limit)


@app.get("/api/history/{session_id}/timeline")
def api_history_timeline(session_id: str, limit: int = 2000) -> dict:
    # Claude Code transcripts
    from core.sessions import PROJECTS_DIR
    for proj_dir in PROJECTS_DIR.iterdir():
        if not proj_dir.is_dir():
            continue
        f = proj_dir / f"{session_id}.jsonl"
        if f.exists():
            fp = str(f)
            events = transcripts.timeline(fp, limit=limit)
            return {
                "session_id": session_id, "project_slug": proj_dir.name,
                "events": events, "platform": "claude",
                "skills_used": transcripts.extract_skills_used(fp),
                "memory_ops": transcripts.extract_memory_ops(fp),
                "plan_history": transcripts.extract_plan_history(fp),
            }
    # Codex transcripts
    from core.codex import CODEX_SESSIONS_DIR
    if CODEX_SESSIONS_DIR.exists():
        for f in CODEX_SESSIONS_DIR.rglob("*.jsonl"):
            if session_id in f.stem:
                events = codex.codex_timeline(str(f), limit=limit)
                return {"session_id": session_id, "project_slug": "codex", "events": events, "platform": "codex"}
    # OpenCode sessions (SQLite)
    try:
        from core.opencode import opencode_timeline
        events = opencode_timeline(session_id, limit=limit)
        if events:
            return {"session_id": session_id, "project_slug": "opencode", "events": events, "platform": "opencode"}
    except Exception:
        pass
    raise HTTPException(404, "transcript not found")


@app.post("/api/history/{session_id}/resume")
def api_history_resume(session_id: str) -> dict:
    import shlex, subprocess
    # If the session is alive, focus it instead of opening a new window.
    for w in sessions.list_windows():
        if w.session_id == session_id and w.alive and w.tty:
            result = actions.focus_terminal(w.tty)
            return {"ok": result.get("ok", False), "action": "focused", "session_id": session_id, "pid": w.pid}

    data = history.list_sessions(limit=9999)
    sess = None
    for s in data["sessions"]:
        if s["session_id"] == session_id:
            sess = s
            break
    if not sess:
        return {"ok": False, "error": "session not found in index"}
    cwd = sess.get("project") or str(Path.home())
    inner = f"cd {shlex.quote(cwd)} && claude --resume {shlex.quote(session_id)}"
    quoted = '"' + inner.replace('\\', '\\\\').replace('"', '\\"') + '"'
    script = f'''tell application "iTerm2"
    activate
    set newWin to (create window with default profile)
    tell current session of newWin
        write text {quoted}
    end tell
end tell'''
    try:
        proc = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, text=True, timeout=10,
        )
    except Exception as e:
        return {"ok": False, "error": str(e)}
    return {"ok": proc.returncode == 0, "action": "resumed", "session_id": session_id, "cwd": cwd}


@app.post("/api/history/{session_id}/fork")
def api_history_fork(session_id: str) -> dict:
    import shlex, subprocess
    data = history.list_sessions(limit=9999)
    sess = None
    for s in data["sessions"]:
        if s["session_id"] == session_id:
            sess = s
            break
    if not sess:
        return {"ok": False, "error": "session not found in index"}
    cwd = sess.get("project") or str(Path.home())
    inner = f"cd {shlex.quote(cwd)} && claude --resume {shlex.quote(session_id)} --fork-session"
    quoted = '"' + inner.replace('\\', '\\\\').replace('"', '\\"') + '"'
    script = f'''tell application "iTerm2"
    activate
    set newWin to (create window with default profile)
    tell current session of newWin
        write text {quoted}
    end tell
end tell'''
    try:
        proc = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, text=True, timeout=10,
        )
    except Exception as e:
        return {"ok": False, "error": str(e)}
    return {"ok": proc.returncode == 0, "action": "forked", "session_id": session_id, "cwd": cwd}


@app.get("/api/skills/{name}/sessions")
def api_skill_sessions(name: str) -> dict:
    """Reverse lookup: which sessions touched this skill, with per-session counts."""
    data = history.list_sessions(limit=9999)
    rows = []
    for s in data["sessions"]:
        bd = s.get("skill_breakdown", {}) or {}
        inv = (bd.get("per_skill_invokes") or {}).get(name, 0)
        rd = (bd.get("per_skill_reads") or {}).get(name, 0)
        wr = (bd.get("per_skill_writes") or {}).get(name, 0)
        bash = (bd.get("per_skill_bash_refs") or {}).get(name, 0)
        total = inv + rd + wr + bash
        if total == 0:
            continue
        rows.append({
            "session_id": s["session_id"],
            "project_name": s["project_name"],
            "platform": s.get("platform", "claude"),
            "title": s.get("first_input", "")[:120],
            "ts": s.get("last_ts") or s.get("first_ts") or "",
            "invoke": inv,
            "reads": rd,
            "writes": wr,
            "bash_refs": bash,
            "total": total,
        })
    rows.sort(key=lambda r: -r["total"])
    return {"name": name, "sessions": rows, "session_count": len(rows)}


@app.get("/api/memory/{name}/sessions")
def api_memory_sessions(name: str) -> dict:
    """Reverse lookup: which sessions read/wrote this memory."""
    data = history.list_sessions(limit=9999)
    rows = []
    for s in data["sessions"]:
        bd = s.get("memory_breakdown", {}) or {}
        rd = (bd.get("per_memory_reads") or {}).get(name, 0)
        wr = (bd.get("per_memory_writes") or {}).get(name, 0)
        ed = (bd.get("per_memory_edits") or {}).get(name, 0)
        total = rd + wr + ed
        if total == 0:
            continue
        rows.append({
            "session_id": s["session_id"],
            "project_name": s["project_name"],
            "platform": s.get("platform", "claude"),
            "title": s.get("first_input", "")[:120],
            "ts": s.get("last_ts") or s.get("first_ts") or "",
            "reads": rd,
            "writes": wr,
            "edits": ed,
            "total": total,
        })
    rows.sort(key=lambda r: -r["total"])
    return {"name": name, "sessions": rows, "session_count": len(rows)}


@app.get("/api/memory/{name}")
def api_memory_detail(name: str) -> dict:
    from core.sessions import PROJECTS_DIR
    for proj_dir in PROJECTS_DIR.iterdir():
        mem_dir = proj_dir / "memory"
        if not mem_dir.is_dir():
            continue
        f = mem_dir / f"{name}.md"
        if f.exists():
            text = f.read_text(errors="replace")
            fm = memory._parse_frontmatter(text) if hasattr(memory, '_parse_frontmatter') else {}
            body_start = text.find("\n---", 3)
            body = text[body_start + 4:].strip() if body_start > 0 else text
            return {
                "name": fm.get("name", name),
                "description": fm.get("description", ""),
                "type": fm.get("type", "unknown"),
                "content": body,
                "path": str(f),
            }
    raise HTTPException(404, "memory not found")


@app.get("/api/skills")
def api_skills() -> dict:
    data = history.list_sessions(limit=9999)
    session_count: dict[str, int] = {}
    invoke_count: dict[str, int] = {}
    reads_count: dict[str, int] = {}
    writes_count: dict[str, int] = {}
    bash_refs_count: dict[str, int] = {}
    for s in data["sessions"]:
        for sk in s.get("skills_used", []):
            session_count[sk] = session_count.get(sk, 0) + 1
        # Use the per-session breakdown that history index already produced
        # (covers Claude + OpenCode + Codex uniformly).
        bd = s.get("skill_breakdown") or {}
        for sk, cnt in (bd.get("per_skill_invokes") or {}).items():
            invoke_count[sk] = invoke_count.get(sk, 0) + cnt
        for sk, cnt in (bd.get("per_skill_reads") or {}).items():
            reads_count[sk] = reads_count.get(sk, 0) + cnt
        for sk, cnt in (bd.get("per_skill_writes") or {}).items():
            writes_count[sk] = writes_count.get(sk, 0) + cnt
        for sk, cnt in (bd.get("per_skill_bash_refs") or {}).items():
            bash_refs_count[sk] = bash_refs_count.get(sk, 0) + cnt
    all_skills = skills.list_all_skills()
    for s in all_skills:
        name = s["name"]
        inv = invoke_count.get(name, 0)
        rd = reads_count.get(name, 0)
        wr = writes_count.get(name, 0)
        brefs = bash_refs_count.get(name, 0)
        s["session_count"] = session_count.get(name, 0)
        s["invoke_count"] = inv
        s["reads"] = rd
        s["writes"] = wr
        s["bash_refs"] = brefs
        s["total_activity"] = inv + rd + wr + brefs
    all_skills.sort(key=lambda s: (-s["total_activity"], -s["invoke_count"], s["name"]))
    return {"skills": all_skills}


@app.get("/api/memory")
def api_memory(project: str | None = None) -> dict:
    data = history.list_sessions(limit=9999)
    read_count: dict[str, int] = {}
    write_count: dict[str, int] = {}
    for s in data["sessions"]:
        for m in s.get("memory_ops", []):
            name = m["name"]
            if m["operation"] == "read":
                read_count[name] = read_count.get(name, 0) + 1
            else:
                write_count[name] = write_count.get(name, 0) + 1
    result = memory.list_memories(project_slug=project)
    for group_mems in result.get("groups", {}).values():
        for m in group_mems:
            stem = m.get("file_stem", m["name"])
            m["read_sessions"] = read_count.get(stem, 0)
            m["write_sessions"] = write_count.get(stem, 0)
    return result


@app.get("/api/perms")
def api_perms() -> dict:
    return perms.snapshot()


@app.get("/api/vscode-active")
def api_vscode_active() -> dict:
    """Shell pid of the user's currently-active VS Code terminal (polled fast by
    the client to ring the matching card without snapshot lag). Just reads one
    small file the companion extension keeps current."""
    return {"pid": vscode.active_shell_pid()}


@app.get("/api/events")
async def api_events(request: Request) -> EventSourceResponse:
    queue: asyncio.Queue = asyncio.Queue(maxsize=32)
    state.subscribers.add(queue)

    async def event_gen():
        # Send the current snapshot once immediately.
        snap = state.last_snapshot or _enriched_snapshot()
        yield {"event": "snapshot", "data": json.dumps(snap)}
        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    payload = await asyncio.wait_for(queue.get(), timeout=20.0)
                    yield {"event": "snapshot", "data": payload}
                except asyncio.TimeoutError:
                    yield {"event": "heartbeat", "data": str(int(time.time()))}
        finally:
            state.subscribers.discard(queue)

    return EventSourceResponse(event_gen())
