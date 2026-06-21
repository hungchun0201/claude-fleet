"""Track Claude Code sessions running on a remote host (e.g. the lab box).

`claude-lab <name>` opens a tmux session `lab-<name>` on the lab machine and runs
Claude Code inside it. That session records the same `~/.claude/sessions/*.json`
+ transcript files as a local one — just on the remote box. We surface them on
the dashboard by SSH-polling those files.

One SSH round-trip per poll fetches every *alive* session's JSON plus a tail of
its transcript; the transcript tails are mirrored to local temp files so the
normal transcripts.py extractors (current task, last input, usage, triage) work
unchanged. Polling runs on a slow background task; the 2-second snapshot only
reads the cached result + local temp files, so it never blocks on the network.
"""
from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path
from typing import Optional

# Hosts to track. Override with CLAUDE_FLEET_REMOTE_HOSTS="lab,other" (ssh aliases).
DEFAULT_HOSTS = ["lab"]
SSH_TIMEOUT_S = 25
# Mirror a generous tail so recent context (current task, last input, usage,
# background tasks, a Workflow launch) survives even on multi-MB transcripts.
# 60KB was far too small — it dropped a Workflow's tool_use while the run was
# still live, mislabelling the session "completed". Parsing ~7 extractors over a
# 2MB tail measures ~13ms, and tail-fetching is one SSH round-trip every 8s, so
# this stays cheap. extract_background_tasks also recovers a workflow from its
# spawn-ack alone, so correctness no longer depends on the run fitting here.
TRANSCRIPT_TAIL_BYTES = 2_000_000

_TMP_ROOT = Path("/tmp/claude-fleet-remote")

# Delimiters the remote script emits so we can split sessions out of one stream.
_SEP_SESSION = "<<<FLEET-SESSION>>>"
_SEP_TMUX = "<<<FLEET-TMUX>>>"
_SEP_TRANSCRIPT = "<<<FLEET-TRANSCRIPT>>>"
_SEP_END = "<<<FLEET-END>>>"

# Runs on the remote host: for each alive session, print its JSON, then whether
# its tmux session currently has a client attached, then a tail of its
# transcript, bracketed by markers. Pure POSIX sh + coreutils + awk.
#
# "attached" comes from the session's OWN tmux (does its pane's tty belong to a
# session with a connected client?) — authoritative even when two sessions share
# a name, unlike guessing from the laptop's `ssh ... claude-lab` process list.
_REMOTE_SCRIPT = r"""
for f in "$HOME"/.claude/sessions/*.json; do
  [ -f "$f" ] || continue
  case "$f" in */session-*) continue;; esac
  pid=$(sed -n 's/.*"pid":\([0-9][0-9]*\).*/\1/p' "$f")
  [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null || continue
  printf '%s\n' "__SEP_SESSION__"
  cat "$f"; printf '\n'
  ctty=$(ps -o tty= -p "$pid" 2>/dev/null | tr -d ' ')
  att=$(tmux list-panes -a -F '#{pane_tty} #{session_attached}' 2>/dev/null \
        | awk -v t="/dev/$ctty" '$1==t{print $2; f=1} END{if(!f)print "?"}')
  printf '%s\n%s\n' "__SEP_TMUX__" "$att"
  sid=$(sed -n 's/.*"sessionId":"\([^"]*\)".*/\1/p' "$f")
  cwd=$(sed -n 's/.*"cwd":"\([^"]*\)".*/\1/p' "$f")
  slug=$(printf '%s' "$cwd" | sed 's#[/_.]#-#g')
  printf '%s\n' "__SEP_TRANSCRIPT__"
  tail -c __TAIL__ "$HOME/.claude/projects/$slug/$sid.jsonl" 2>/dev/null
  printf '\n%s\n' "__SEP_END__"
done
"""


def _hosts() -> list[str]:
    raw = os.environ.get("CLAUDE_FLEET_REMOTE_HOSTS")
    if raw is not None:
        return [h.strip() for h in raw.split(",") if h.strip()]
    return DEFAULT_HOSTS


def _build_script() -> str:
    return (
        _REMOTE_SCRIPT
        .replace("__SEP_SESSION__", _SEP_SESSION)
        .replace("__SEP_TMUX__", _SEP_TMUX)
        .replace("__SEP_TRANSCRIPT__", _SEP_TRANSCRIPT)
        .replace("__SEP_END__", _SEP_END)
        .replace("__TAIL__", str(TRANSCRIPT_TAIL_BYTES))
    )


def _ssh_fetch(host: str) -> Optional[str]:
    """Remote stdout on success (possibly "" when no sessions), or None on a real
    SSH failure. ControlPath=none keeps this poll on its own connection so the
    user's interactive ssh/Ctrl-C can't drag it down via a shared master."""
    try:
        proc = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=12",
             "-o", "ControlPath=none", host, "sh", "-s"],
            input=_build_script(), capture_output=True, text=True,
            errors="replace", timeout=SSH_TIMEOUT_S,
        )
    except Exception:
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout


def _cwd_slug(cwd: str) -> str:
    return cwd.replace("/", "-").replace("_", "-").replace(".", "-")


def _session_blocks(stream: str):
    """Yield each session's lines (everything after a bare _SEP_SESSION line, up
    to the next one). Splitting on the whole-line marker — never a substring —
    means a marker literal inside a JSON field or transcript line can't start a
    spurious block."""
    block: Optional[list[str]] = None
    for line in stream.split("\n"):
        if line == _SEP_SESSION:
            if block is not None:
                yield block
            block = []
        elif block is not None:
            block.append(line)
    if block is not None:
        yield block


def _parse(host: str, stream: str) -> list[dict]:
    out: list[dict] = []
    host_tmp = _TMP_ROOT / host
    host_tmp.mkdir(parents=True, exist_ok=True)
    now_ms = int(time.time() * 1000)

    for lines in _session_blocks(stream):
        # Segment by WHOLE-LINE markers: <json> [SEP_TMUX <flag>] SEP_TRANSCRIPT
        # <tail> SEP_END. The remote script always prints each marker on its own
        # line, so the same literal occurring inside the JSON or the transcript
        # tail (e.g. a lab session editing this repo) can't mis-split the block.
        # SEP_TMUX is optional, so a pre-upgrade remote stream still parses.
        seg = "json"
        parts: dict[str, list[str]] = {"json": [], "tmux": [], "tx": []}
        for line in lines:
            if line == _SEP_TMUX:
                seg = "tmux"
            elif line == _SEP_TRANSCRIPT:
                seg = "tx"
            elif line == _SEP_END:
                seg = "done"
            elif seg in parts:
                parts[seg].append(line)
        try:
            data = json.loads("\n".join(parts["json"]).strip())
        except Exception:
            continue
        if not isinstance(data, dict) or "sessionId" not in data:
            continue
        transcript = "\n".join(parts["tx"])
        # "1" attached / "0" detached / "?" or "" unknown (no tmux match -> None,
        # so the caller falls back to the local-attachment heuristic).
        flag = "\n".join(parts["tmux"]).strip()
        tmux_attached = True if flag == "1" else (False if flag == "0" else None)

        # Only track claude-lab sessions (tmux/window named lab-*); other lab
        # claude processes (orphans, direct ssh) are noise here.
        name = data.get("name")
        if not (isinstance(name, str) and name.startswith("lab-")):
            continue

        sid = data["sessionId"]
        cwd = data.get("cwd", "")
        # Mirror the transcript tail locally so transcripts.py works unchanged.
        tp: Optional[str] = None
        body = transcript.strip("\n")
        if body.strip():
            tf = host_tmp / f"{sid}.jsonl"
            try:
                tf.write_text(body + "\n")
                tp = str(tf)
            except OSError:
                tp = None

        out.append({
            "pid": int(data.get("pid", 0)),
            "session_id": sid,
            "cwd": cwd,
            "project_name": os.path.basename(cwd) or cwd,
            "project_slug": _cwd_slug(cwd),
            "name": data.get("name"),
            "status": data.get("status", "unknown"),
            "waiting_for": data.get("waitingFor"),
            "started_at": int(data.get("startedAt", 0)),
            "updated_at": int(data.get("updatedAt", 0)),
            "version": str(data.get("version", "")),
            "tty": None,
            "transcript_path": tp,
            "alive": True,
            "remote": True,
            "host": host,
            "tmux": data.get("name"),  # claude-lab names the tmux session == session name
            "tmux_attached": tmux_attached,  # authoritative attach state (None = unknown)
            "idle_seconds": max(0, int(time.time() - data.get("updatedAt", now_ms) / 1000)),
        })
    return out


def poll() -> tuple[bool, list[dict]]:
    """Fetch alive remote sessions across all hosts (blocking).

    Returns (ok, windows). `ok` is True when every host's SSH succeeded — even
    if it found zero sessions. An empty list with ok=True means the sessions
    genuinely ended (clear them); ok=False means a host was unreachable (keep
    the last-known sessions, marked stale).
    """
    windows: list[dict] = []
    ok = True
    for host in _hosts():
        stream = _ssh_fetch(host)
        if stream is None:
            ok = False
            continue
        windows.extend(_parse(host, stream))
    return ok, windows


def _shq(s: str) -> str:
    """POSIX single-quote a string so the remote shell treats it as a literal."""
    return "'" + s.replace("'", "'\\''") + "'"


def _ssh_cat(host: str, slug: str, sid: str) -> Optional[str]:
    """Remote transcript contents, or None on failure. ssh flattens argv into one
    string and re-parses it through the remote login shell, so slug/sid are
    single-quoted into the command (injection-safe) while $HOME stays unquoted to
    expand remotely. ControlPath=none keeps it off any shared master the user's
    interactive ssh might own."""
    remote_cmd = f'cat "$HOME"/.claude/projects/{_shq(slug)}/{_shq(sid)}.jsonl'
    try:
        proc = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=12",
             "-o", "ControlPath=none", host, remote_cmd],
            capture_output=True, text=True, errors="replace", timeout=SSH_TIMEOUT_S,
        )
    except Exception:
        return None
    if proc.returncode != 0 or not proc.stdout:
        return None
    return proc.stdout


def fetch_full_transcript(host: Optional[str], cwd: Optional[str],
                          session_id: Optional[str]) -> Optional[str]:
    """SSH-fetch the COMPLETE remote transcript for a session, mirror it under
    <tmp>/<host>/full/, and return the local path — or None if the host is
    unreachable or the file is missing. Unlike the poll's 60KB tail, this is the
    whole history, fetched on demand when the user opens a lab card's timeline."""
    if not (host and cwd and session_id):
        return None
    body = _ssh_cat(host, _cwd_slug(cwd), session_id)
    if not body:
        return None
    full_dir = _TMP_ROOT / host / "full"
    try:
        full_dir.mkdir(parents=True, exist_ok=True)
        fp = full_dir / f"{session_id}.jsonl"
        fp.write_text(body if body.endswith("\n") else body + "\n")
    except OSError:
        return None
    return str(fp)


def local_attachment_pid(name: Optional[str], ps_info: dict) -> Optional[int]:
    """The local pid running `claude-lab <handle>` for a `lab-<suffix>` session,
    i.e. the laptop terminal whose VS Code shell we focus on a card click. None
    when no such terminal is found.

    Matches on the *handle* (the first token after the `claude-lab` binary) —
    `claude-lab <handle> [dir]` — so a second positional arg that happens to be a
    same-named directory can't masquerade as an attachment. The handle matches
    when the session suffix starts with it (exact, or the prefix the lab-side
    script's prefix-match would have resolved). Whether a terminal is *really*
    attached is decided by the remote tmux (`tmux_attached`); this only resolves
    which local terminal to focus.

    `ps_info` is vscode._ps_parents() output: {pid: (ppid, command)}.
    """
    if not name or not name.startswith("lab-"):
        return None
    suffix = name[len("lab-"):]
    for pid, (_ppid, cmd) in ps_info.items():
        if "claude-lab" not in cmd:
            continue
        parts = cmd.split()
        handle = None
        for i, tok in enumerate(parts):
            if "claude-lab" in tok:
                if i + 1 < len(parts):
                    handle = parts[i + 1]
                break
        if (handle and suffix.startswith(handle)) or name in parts:
            return pid
    return None
