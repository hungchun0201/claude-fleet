// Claude Fleet Focus — companion extension.
//
// VS Code has no external API to focus a specific integrated terminal nor to
// tell which one is active, so this tiny extension bridges both over files in
// ~/.config/claude-fleet/:
//
//   vscode-focus     (dashboard writes {"pid": <shellPid>})  -> we focus that terminal
//   vscode-active    (we write {"pid": <activeShellPid>})    -> dashboard highlights it
//   vscode-reattach  (dashboard appends {jobs:[...]})        -> open a terminal for the cmd
//   vscode-lastfocus (we write {"token": <winId>})           -> which window opens it
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
const REATTACH = path.join(DIR, 'vscode-reattach');
const CLAIMS = path.join(DIR, 'reattach-claims');
const LASTFOCUS = path.join(DIR, 'vscode-lastfocus');
const REATTACH_TTL_MS = 120000;

// A reattach terminal opens in the window the user was most recently in. Each
// window runs its own extension-host process; this is a stable id for THIS
// window, written to LASTFOCUS whenever the window gains focus. Clicking the
// dashboard (a browser) defocuses VS Code, so the right target is the LAST
// focused window, not whichever happens to be focused at claim time. (pid +
// entropy so the id is unique per window even if a pid is later recycled.)
const WIN_TOKEN = process.pid + '-' + Date.now() + '-' + Math.floor(Math.random() * 1e6);

function activate(context) {
  try { fs.mkdirSync(DIR, { recursive: true }); } catch (e) {}
  try { fs.mkdirSync(CLAIMS, { recursive: true }); } catch (e) {}

  // Record this window as the most-recently-focused one (handleReattach below
  // only opens a terminal in the window whose token is the latest here). When
  // unfocused we normally leave the file alone, but if NO window holds the role
  // yet we bootstrap it — so a single window that wasn't focused at startup
  // still receives reattach terminals.
  function markFocus() {
    if (!vscode.window.state.focused) {
      try { fs.accessSync(LASTFOCUS); return; } catch (e) { /* missing -> claim it */ }
    }
    try { fs.writeFileSync(LASTFOCUS, JSON.stringify({ token: WIN_TOKEN, ts: Date.now() })); } catch (e) {}
  }

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

  // --- inbound: open a reattach terminal in THIS window, but only if THIS is the
  // most-recently-focused window (the one the user was just in) — so the terminal
  // lands where they're looking, not duplicated across every open window. The
  // wx-flag claim marker makes each job run exactly once even across windows.
  function handleReattach() {
    let lf;
    try { lf = JSON.parse(fs.readFileSync(LASTFOCUS, 'utf8')); } catch (e) { return; }
    if (!lf || String(lf.token) !== WIN_TOKEN) return; // not the user's window
    let jobs;
    try { jobs = (JSON.parse(fs.readFileSync(REATTACH, 'utf8')) || {}).jobs; } catch (e) { return; }
    if (!Array.isArray(jobs)) return;
    const now = Date.now();
    for (const job of jobs) {
      if (!job || !job.id || !job.cmd) continue;
      // job.id is a claim-marker filename; re-validate so a hand-edited queue
      // file can't path-traverse.
      if (!/^[A-Za-z0-9._-]+$/.test(job.id)) continue;
      if (now - (Number(job.ts) || 0) > REATTACH_TTL_MS) continue; // expired
      const marker = path.join(CLAIMS, job.id);
      try {
        fs.writeFileSync(marker, String(now), { flag: 'wx' }); // run each job once
      } catch (e) {
        continue; // EEXIST — already opened
      }
      const term = vscode.window.createTerminal({ name: 'lab ▸ ' + (job.label || 'reattach') });
      term.sendText(job.cmd);
      term.show(false); // adds a tab to this window's terminal panel and reveals it
    }
  }

  // Best-effort sweep of stale claim markers so the dir doesn't grow forever.
  try {
    for (const f of fs.readdirSync(CLAIMS)) {
      const p = path.join(CLAIMS, f);
      try {
        if (Date.now() - fs.statSync(p).mtimeMs > REATTACH_TTL_MS * 4) fs.unlinkSync(p);
      } catch (e) {}
    }
  } catch (e) {}

  // --- outbound: report which terminal is currently active ---
  async function reportActive() {
    const t = vscode.window.activeTerminal;
    let pid = null;
    if (t) { try { pid = await t.processId; } catch (e) {} }
    try { fs.writeFileSync(ACTIVE, JSON.stringify({ pid: pid, ts: Date.now() })); } catch (e) {}
  }

  // fs.watch is unreliable on macOS, so poll the focus + reattach requests too.
  try {
    const w = fs.watch(DIR, (_evt, name) => {
      if (name === path.basename(REQUEST)) handleFocus();
      else if (name === path.basename(REATTACH)) handleReattach();
    });
    context.subscriptions.push({ dispose: () => w.close() });
  } catch (e) {}
  const timer = setInterval(() => { handleFocus(); handleReattach(); }, 1000);
  context.subscriptions.push({ dispose: () => clearInterval(timer) });

  context.subscriptions.push(vscode.window.onDidChangeActiveTerminal(reportActive));
  context.subscriptions.push(vscode.window.onDidCloseTerminal(reportActive));
  context.subscriptions.push(vscode.window.onDidChangeWindowState(markFocus));

  markFocus();
  handleFocus();
  handleReattach();
  reportActive();
}

function deactivate() {
  try { fs.writeFileSync(ACTIVE, JSON.stringify({ pid: null, ts: Date.now() })); } catch (e) {}
}

module.exports = { activate, deactivate };
