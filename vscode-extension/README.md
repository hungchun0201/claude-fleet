# Claude Fleet Focus

Companion extension for the [Claude Fleet](https://github.com/hungchun0201/claude-fleet)
dashboard. When you click **Focus** on a session that lives in a VS Code
integrated terminal, the dashboard writes that terminal's shell PID to
`~/.config/claude-fleet/vscode-focus`; this extension matches it against
`vscode.window.terminals[].processId` and calls `terminal.show()` to bring the
right terminal tab to the front.

No configuration, no ports — it just watches that one file.

## Install

```bash
# from the claude-fleet repo root
bash scripts/install-vscode-extension.sh
```

Then reload the window (⇧⌘P → *Developer: Reload Window*). Integrated terminals
persist across the reload.
