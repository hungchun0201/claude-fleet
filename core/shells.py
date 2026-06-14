"""Inspect the live background shells a Claude Code session left running.

`status == "shell"` tells us a shell is alive but not *what* it is doing. Claude
Code launches every Bash-tool shell as `/bin/zsh -c source <shell-snapshot> …`,
so a lingering one is a descendant of the session pid whose command carries the
shell-snapshots/snapshot- signature. We report the child program each such shell
is actually running (its most informative child) — or that it is idle with no
child (the genuinely dead/forgotten shell). MCP servers and other infra children
lack the signature and are naturally excluded.

Detection is live (a single `ps` snapshot) rather than transcript-based: shells
the harness auto-backgrounds (e.g. a piped command) never appear in the
transcript as run_in_background, so only the process tree knows the truth.
"""
from __future__ import annotations

import collections
import subprocess
from typing import Optional

SNAPSHOT_SIG = "shell-snapshots/snapshot-"

# A genuinely lingering shell has been alive a while; a transient command shell
# (a quick foreground command caught during the busy→shell status flip) is only
# seconds old. Ignore anything younger than this so diagnostics/hooks don't show
# up as "background shells" (and can't pollute the snapshot).
MIN_SHELL_AGE_S = 15


def _etime_seconds(etime: str) -> int:
    """Parse `ps` ELAPSED ([[DD-]HH:]MM:SS) to seconds; 0 on any oddity."""
    try:
        days = 0
        if "-" in etime:
            d, etime = etime.split("-", 1)
            days = int(d)
        parts = [int(x) for x in etime.split(":")]
        if len(parts) == 3:
            h, m, s = parts
        elif len(parts) == 2:
            h, m, s = 0, parts[0], parts[1]
        else:
            return 0
        return days * 86400 + h * 3600 + m * 60 + s
    except (ValueError, AttributeError):
        return 0


def _ps_rows() -> list[tuple[int, int, str, str]]:
    """(pid, ppid, etime, command) for every process, or [] on failure."""
    try:
        out = subprocess.check_output(
            ["ps", "-eo", "pid=,ppid=,etime=,command="],
            stderr=subprocess.DEVNULL, timeout=3,
        ).decode(errors="replace")
    except Exception:
        return []
    rows: list[tuple[int, int, str, str]] = []
    for line in out.splitlines():
        parts = line.split(None, 3)
        if len(parts) < 4:
            continue
        try:
            pid, ppid = int(parts[0]), int(parts[1])
        except ValueError:
            continue
        rows.append((pid, ppid, parts[2], parts[3]))
    return rows


def _clean(cmd: str) -> str:
    # Keep the whole command (collapsed whitespace) so the card can show what the
    # shell is actually running; the frontend wraps it rather than clipping.
    return " ".join(cmd.split())[:400]


def background_shells(session_pid: int, rows: Optional[list] = None) -> list[dict]:
    """Live background shells under a session, with what each is running.

    Returns [{pid, elapsed, doing}], where `doing` is the cleaned command of the
    shell's main child program, or None when the shell sits idle with no child.
    """
    rows = rows if rows is not None else _ps_rows()
    if not rows:
        return []

    children: dict[int, list[int]] = collections.defaultdict(list)
    info: dict[int, tuple[int, str, str]] = {}
    for pid, ppid, etime, cmd in rows:
        children[ppid].append(pid)
        info[pid] = (ppid, etime, cmd)

    # Collect every descendant of the session pid (BFS over the tree).
    seen: set[int] = set()
    stack = [session_pid]
    descendants: list[int] = []
    while stack:
        for ch in children.get(stack.pop(), []):
            if ch in seen:
                continue
            seen.add(ch)
            descendants.append(ch)
            stack.append(ch)

    out: list[dict] = []
    for pid in descendants:
        _, etime, cmd = info[pid]
        if SNAPSHOT_SIG not in cmd:
            continue  # not a Claude Code Bash-tool shell (MCP/infra/etc.)
        if _etime_seconds(etime) < MIN_SHELL_AGE_S:
            continue  # transient command shell, not a lingering one
        # What is this shell running? Its most informative non-shell child.
        kid_cmds = [
            info[k][2] for k in children.get(pid, [])
            if SNAPSHOT_SIG not in info[k][2]
        ]
        doing = _clean(max(kid_cmds, key=len)) if kid_cmds else None
        out.append({"pid": pid, "elapsed": etime, "doing": doing})
    return out
