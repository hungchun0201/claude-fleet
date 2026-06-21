"""Focus a Claude session running inside a VS Code-family integrated terminal.

VS Code exposes no external API to focus a specific integrated terminal, and its
accessibility tree doesn't surface the tabs — so a tiny companion extension
(see vscode-extension/) does the actual focusing. The dashboard only:

  1. resolves the session's terminal shell PID (the process whose parent is the
     editor's Electron pty host), and writes it to a request file, and
  2. activates the editor's .app window.

The extension watches the request file, matches the PID against
vscode.window.terminals[].processId, and calls terminal.show(). Works for
VS Code, Cursor, VSCodium, etc. — anything Electron-based with this layout.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Optional

_CONFIG_DIR = Path.home() / ".config" / "claude-fleet"
REQUEST_FILE = _CONFIG_DIR / "vscode-focus"
ACTIVE_FILE = _CONFIG_DIR / "vscode-active"
# Reattach queue: the dashboard appends a job here; a freshly-opened editor
# window's companion extension claims it and runs the command in a terminal.
REATTACH_FILE = _CONFIG_DIR / "vscode-reattach"
# The extension drops a claim marker here (named by job id) when it runs a job.
REATTACH_CLAIMS_DIR = _CONFIG_DIR / "reattach-claims"
# A job older than this is ignored by the extension (so a window opened much
# later for unrelated reasons never runs a stale reattach).
REATTACH_TTL_S = 120
# claude-lab session suffixes are project-derived slugs; reject anything else so
# the string typed into the terminal can't carry shell metacharacters.
_SUFFIX_RE = re.compile(r"^[A-Za-z0-9._-]+$")
# Reattach job ids are machine-generated to this charset (also a claim-marker
# filename); validate before any filesystem lookup from a client-supplied id.
_JOB_ID_RE = re.compile(r"^[A-Za-z0-9._-]+$")

# An Electron pty host runs from inside the app bundle's Frameworks dir
# (e.g. ".../Visual Studio Code 2.app/Contents/Frameworks/Code Helper (Plugin).app/...").
_HOST_MARK = ".app/Contents/Frameworks/"


def _ps_parents() -> dict[int, tuple[int, str]]:
    """{pid: (ppid, command)} for every process."""
    try:
        # errors="replace": a single process with a non-UTF-8 byte in its command
        # line must not blow up the whole table (strict decode → {} → every focus
        # / shell-pid / attachment lookup silently fails).
        out = subprocess.check_output(
            ["ps", "-axo", "pid=,ppid=,command="], text=True, errors="replace", timeout=5,
        )
    except Exception:
        return {}
    info: dict[int, tuple[int, str]] = {}
    for line in out.splitlines():
        parts = line.split(None, 2)
        if len(parts) < 3:
            continue
        try:
            info[int(parts[0])] = (int(parts[1]), parts[2])
        except ValueError:
            continue
    return info


def _app_name(cmd: str) -> Optional[str]:
    """'/Applications/Visual Studio Code 2.app/...' -> 'Visual Studio Code 2'."""
    i = cmd.find(".app/")
    if i < 0:
        return None
    return cmd[:i].rsplit("/", 1)[-1] or None


def detect(pid: int, info: Optional[dict] = None) -> Optional[dict]:
    """If `pid` runs in a VS Code integrated terminal, return
    {"shell_pid", "app"}; otherwise None.

    The terminal's shell is the first ancestor whose *parent* is the editor's
    pty host — that shell's pid is what the editor reports as terminal.processId.
    """
    info = info if info is not None else _ps_parents()
    cur = pid
    for _ in range(15):
        if cur not in info:
            break
        ppid, _cmd = info[cur]
        parent_cmd = info.get(ppid, (0, ""))[1]
        if _HOST_MARK in parent_cmd:
            return {"shell_pid": cur, "app": _app_name(parent_cmd)}
        cur = ppid
    return None


def active_shell_pid() -> Optional[int]:
    """The shell pid of the terminal the user currently has active in the editor,
    as last reported by the companion extension (None if unknown)."""
    try:
        data = json.loads(ACTIVE_FILE.read_text())
    except (OSError, ValueError):
        return None
    pid = data.get("pid")
    return pid if isinstance(pid, int) else None


def focus(pid: int) -> Optional[dict]:
    """Focus the VS Code terminal hosting `pid`. Returns a result dict, or None
    if `pid` is not VS Code-hosted (so the caller can fall back)."""
    d = detect(pid)
    if not d:
        return None
    try:
        REQUEST_FILE.parent.mkdir(parents=True, exist_ok=True)
        REQUEST_FILE.write_text(
            json.dumps({"pid": d["shell_pid"], "ts": int(time.time() * 1000)})
        )
    except OSError as e:
        return {"ok": False, "error": f"could not write focus request: {e}"}
    app = d.get("app")
    if app:
        try:
            subprocess.run(
                ["osascript", "-e", f'tell application "{app}" to activate'],
                capture_output=True, timeout=6,
            )
        except Exception:
            pass  # window stays where it is; the terminal still gets focused
    return {"ok": True, "via": "vscode", "app": app, "shell_pid": d["shell_pid"]}


# --------------------------------------------------------------------------- #
# Reattach a detached remote (lab) session as a terminal in the user's window.
#
# A detached `claude-lab` session is a tmux still alive on the remote with no
# local terminal attached. To reattach we queue a job describing the
# `claude-lab <suffix>` command; the companion extension, running in the
# most-recently-focused editor window, opens a new terminal tab there and runs
# it — that interactive terminal is the only place `claude-lab` (a shell alias)
# exists. No new window is spawned: the terminal lands where the user already is.
# --------------------------------------------------------------------------- #


def _read_reattach_jobs() -> list[dict]:
    try:
        data = json.loads(REATTACH_FILE.read_text())
    except (OSError, ValueError):
        return []
    jobs = data.get("jobs") if isinstance(data, dict) else None
    if not isinstance(jobs, list):
        return []
    return [j for j in jobs if isinstance(j, dict) and j.get("id") and j.get("cmd")]


def _write_reattach_jobs(jobs: list[dict]) -> None:
    REATTACH_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = REATTACH_FILE.with_name(REATTACH_FILE.name + ".tmp")
    tmp.write_text(json.dumps({"jobs": jobs}))
    tmp.replace(REATTACH_FILE)  # atomic publish


def _job_consumed(job_id: str) -> bool:
    """A job whose claim marker exists was already run by some window."""
    if not job_id:
        return True
    try:
        return (REATTACH_CLAIMS_DIR / job_id).exists()
    except OSError:
        return False


def reattach_claimed(job_id: str) -> bool:
    """True once an editor window has claimed (opened a terminal for) this job.
    Rejects an unsafe id rather than touching the filesystem with it."""
    if not job_id or not _JOB_ID_RE.match(job_id):
        return False
    return _job_consumed(job_id)


def queue_reattach_job(cmd: str, label: str, now_ms: Optional[int] = None) -> str:
    """Append a reattach job and return its id. Drops jobs that are expired or
    already consumed (claim marker present) so a stale entry can never be re-run
    by a later window. The id is filename-safe (the extension claims it via a
    marker file named by id)."""
    now = now_ms if now_ms is not None else int(time.time() * 1000)
    keep = [j for j in _read_reattach_jobs()
            if now - int(j.get("ts", 0)) < REATTACH_TTL_S * 1000
            and not _job_consumed(str(j.get("id", "")))]
    jid = re.sub(r"[^A-Za-z0-9._-]", "_", f"{now}-{os.getpid()}-{label}")
    keep.append({"id": jid, "cmd": cmd, "label": label, "ts": now})
    _write_reattach_jobs(keep)
    return jid


def reattach_remote(suffix: str, label: Optional[str] = None,
                    host: Optional[str] = None, cwd: Optional[str] = None) -> dict:
    """Queue a reattach for the detached lab tmux `lab-<suffix>`. The companion
    extension opens a terminal in the user's most-recently-focused window and
    runs `claude-lab <suffix>` there. Returns a result dict."""
    suffix = (suffix or "").strip()
    if not _SUFFIX_RE.match(suffix):
        return {"ok": False, "error": f"unsafe session suffix: {suffix!r}"}
    cmd = f"claude-lab {suffix}"
    jid = queue_reattach_job(cmd, label or f"lab-{suffix}")
    return {"ok": True, "via": "vscode-reattach", "job_id": jid,
            "host": host, "cmd": cmd}
