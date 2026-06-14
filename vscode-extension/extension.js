// Claude Fleet Focus — companion extension.
//
// VS Code has no external API to focus a specific integrated terminal, so the
// dashboard writes the target terminal's shell PID to
//   ~/.config/claude-fleet/vscode-focus   (JSON: {"pid": <shellPid>, "ts": ...})
// and this extension, which sees vscode.window.terminals, matches the PID
// against terminal.processId and calls terminal.show(). No ports, no config.

const vscode = require('vscode');
const fs = require('fs');
const os = require('os');
const path = require('path');

const REQUEST = path.join(os.homedir(), '.config', 'claude-fleet', 'vscode-focus');

function activate(context) {
  let lastSeen = '';

  async function handle() {
    let raw;
    try { raw = fs.readFileSync(REQUEST, 'utf8').trim(); } catch (e) { return; }
    if (!raw || raw === lastSeen) return;
    lastSeen = raw;

    let pid;
    try { pid = JSON.parse(raw).pid; } catch (e) { pid = parseInt(raw, 10); }
    if (!pid) return;

    for (const term of vscode.window.terminals) {
      let tpid;
      try { tpid = await term.processId; } catch (e) { continue; }
      if (tpid === pid) {
        term.show(false); // focus this terminal (preserveFocus = false)
        return;
      }
    }
  }

  // fs.watch is unreliable on macOS (atomic writes, missed events), so poll too.
  try { fs.mkdirSync(path.dirname(REQUEST), { recursive: true }); } catch (e) {}
  try {
    const watcher = fs.watch(path.dirname(REQUEST), (_evt, name) => {
      if (name === path.basename(REQUEST)) handle();
    });
    context.subscriptions.push({ dispose: () => watcher.close() });
  } catch (e) { /* fall back to polling only */ }

  const timer = setInterval(handle, 1000);
  context.subscriptions.push({ dispose: () => clearInterval(timer) });
  handle();
}

function deactivate() {}

module.exports = { activate, deactivate };
