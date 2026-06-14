// Claude Fleet Focus — companion extension.
//
// VS Code has no external API to focus a specific integrated terminal nor to
// tell which one is active, so this tiny extension bridges both over files in
// ~/.config/claude-fleet/:
//
//   vscode-focus   (dashboard writes {"pid": <shellPid>})  -> we focus that terminal
//   vscode-active  (we write {"pid": <activeShellPid>})    -> dashboard highlights it
//
// PIDs are terminal.processId (the shell pid), which the dashboard resolves from
// each session's process tree. No ports, no settings.

const vscode = require('vscode');
const fs = require('fs');
const os = require('os');
const path = require('path');

const DIR = path.join(os.homedir(), '.config', 'claude-fleet');
const REQUEST = path.join(DIR, 'vscode-focus');
const ACTIVE = path.join(DIR, 'vscode-active');

function activate(context) {
  try { fs.mkdirSync(DIR, { recursive: true }); } catch (e) {}

  // --- inbound: focus the terminal the dashboard asks for ---
  let lastSeen = '';
  async function handleFocus() {
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
      if (tpid === pid) { term.show(false); return; }
    }
  }

  // --- outbound: report which terminal is currently active ---
  async function reportActive() {
    const t = vscode.window.activeTerminal;
    let pid = null;
    if (t) { try { pid = await t.processId; } catch (e) {} }
    try { fs.writeFileSync(ACTIVE, JSON.stringify({ pid: pid, ts: Date.now() })); } catch (e) {}
  }

  // fs.watch is unreliable on macOS, so poll the focus request too.
  try {
    const w = fs.watch(DIR, (_evt, name) => { if (name === path.basename(REQUEST)) handleFocus(); });
    context.subscriptions.push({ dispose: () => w.close() });
  } catch (e) {}
  const timer = setInterval(handleFocus, 1000);
  context.subscriptions.push({ dispose: () => clearInterval(timer) });

  context.subscriptions.push(vscode.window.onDidChangeActiveTerminal(reportActive));
  context.subscriptions.push(vscode.window.onDidCloseTerminal(reportActive));

  handleFocus();
  reportActive();
}

function deactivate() {
  try { fs.writeFileSync(ACTIVE, JSON.stringify({ pid: null, ts: Date.now() })); } catch (e) {}
}

module.exports = { activate, deactivate };
