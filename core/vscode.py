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
import subprocess
import time
from pathlib import Path
from typing import Optional

REQUEST_FILE = Path.home() / ".config" / "claude-fleet" / "vscode-focus"
ACTIVE_FILE = Path.home() / ".config" / "claude-fleet" / "vscode-active"

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
