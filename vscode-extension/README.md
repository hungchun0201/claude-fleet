# Claude Fleet Focus

Companion extension for the [Claude Fleet](https://github.com/hungchun0201/claude-fleet)
dashboard. When you click **Focus** on a session that lives in a VS Code
integrated terminal, the dashboard writes that terminal's shell PID to
`~/.config/claude-fleet/vscode-focus`; this extension matches it against
`vscode.window.terminals[].processId` and calls `terminal.show()` to bring the
right terminal tab to the front.

It also handles **Reattach**: when you reattach a detached lab session, the
dashboard appends a job to `~/.config/claude-fleet/vscode-reattach`. This
extension, running in your **most-recently-focused** window (it records that in
`vscode-lastfocus` on focus), opens a new terminal there and runs
`claude-lab <suffix>` — so the terminal lands in the window you're already in,
not a new one. An exclusive marker file makes each job run exactly once.

No configuration, no ports — it just watches those files.

## Install

```bash
# from the claude-fleet repo root
bash scripts/install-vscode-extension.sh
```

Then reload the window (⇧⌘P → *Developer: Reload Window*). Integrated terminals
persist across the reload.
