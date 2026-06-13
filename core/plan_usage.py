"""Live plan usage from the user's own Claude account (read-only, best-effort).

Mirrors Claude.ai → Settings → Usage and Claude Code's `/usage`: the rolling
5-hour session limit plus the weekly all-models and Sonnet-only caps, as the
real utilization % and reset times — not a token estimate.

Source: GET https://api.anthropic.com/api/oauth/usage, authenticated with the
OAuth token Claude Code keeps in the macOS keychain ("Claude Code-credentials").

This is an undocumented, OAuth-gated endpoint. We use the user's *own* token,
read-only, against their own account; we never refresh it (Claude Code keeps the
keychain token fresh while it runs), never log or surface it, cache aggressively,
and degrade silently to None on any failure. Disable entirely by setting
CLAUDE_FLEET_PLAN_USAGE=0.

Network access happens off the request path: app.py refreshes this on a slow
background poller and the snapshot reads the cached value, so the 2-second
dashboard loop never blocks on the keychain or the network.
"""
from __future__ import annotations

import json
import os
import subprocess
import time
import urllib.error
import urllib.request
from typing import Optional

from core import sessions

_KEYCHAIN_SERVICE = "Claude Code-credentials"
_ENDPOINT = "https://api.anthropic.com/api/oauth/usage"
_BETA = "oauth-2025-04-20"
_UA_FALLBACK = "claude-code/2.1.177"

_CACHE_TTL_S = 300      # success: the windows move slowly; don't hammer the API
_FAIL_TTL_S = 120       # failure: back off before retrying

_cache: dict = {"ts": 0.0, "data": None, "ok": False}


def _enabled() -> bool:
    return os.environ.get("CLAUDE_FLEET_PLAN_USAGE", "1") not in ("0", "false", "no")


def _user_agent() -> str:
    """Track the running Claude Code version so the OAuth gate accepts us."""
    try:
        files = sorted(
            sessions.SESSIONS_DIR.glob("*.json"),
            key=lambda p: p.stat().st_mtime, reverse=True,
        )
        for f in files[:5]:
            if f.name.startswith("session-"):
                continue
            v = (json.loads(f.read_text()) or {}).get("version")
            if v:
                return f"claude-code/{v}"
    except Exception:
        pass
    return _UA_FALLBACK


def _read_token() -> Optional[str]:
    """Current OAuth access token from the keychain, or None if unreadable/expired."""
    try:
        out = subprocess.run(
            ["security", "find-generic-password", "-s", _KEYCHAIN_SERVICE, "-w"],
            capture_output=True, text=True, timeout=5,
        )
    except Exception:
        return None
    if out.returncode != 0 or not out.stdout.strip():
        return None
    try:
        creds = json.loads(out.stdout.strip())
    except Exception:
        return None
    oauth = creds.get("claudeAiOauth") or creds
    tok = oauth.get("accessToken")
    exp = oauth.get("expiresAt")  # epoch ms
    if isinstance(exp, (int, float)) and exp / 1000 < time.time():
        return None  # stale; Claude Code will refresh it, just try again later
    return tok if isinstance(tok, str) and tok else None


def _win(d: object) -> Optional[dict]:
    """Normalize one usage window {utilization, resets_at} → {pct, resets_at}."""
    if not isinstance(d, dict):
        return None
    u = d.get("utilization")
    if u is None:
        return None
    try:
        return {"pct": round(float(u), 1), "resets_at": d.get("resets_at")}
    except (TypeError, ValueError):
        return None


def _shape(raw: dict) -> dict:
    return {
        "five_hour": _win(raw.get("five_hour")),
        "seven_day": _win(raw.get("seven_day")),
        "seven_day_sonnet": _win(raw.get("seven_day_sonnet")),
        "seven_day_opus": _win(raw.get("seven_day_opus")),
        "tier": (raw.get("oauth_account") or {}).get("rate_limit_tier"),
    }


def _fetch() -> Optional[dict]:
    tok = _read_token()
    if not tok:
        return None
    req = urllib.request.Request(_ENDPOINT, headers={
        "Authorization": f"Bearer {tok}",
        "anthropic-beta": _BETA,
        "User-Agent": _user_agent(),
        "Content-Type": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            raw = json.load(r)
    except (urllib.error.URLError, TimeoutError, ValueError, OSError):
        return None
    if not isinstance(raw, dict):
        return None
    return _shape(raw)


def refresh() -> Optional[dict]:
    """Fetch (TTL-gated) and update the cache. Blocking — call off the hot path."""
    if not _enabled():
        return None
    now = time.time()
    ttl = _CACHE_TTL_S if _cache["ok"] else _FAIL_TTL_S
    if _cache["ts"] and (now - _cache["ts"]) < ttl:
        return _cache["data"]
    data = _fetch()
    _cache.update(ts=now, data=data, ok=data is not None)
    return data


def cached() -> Optional[dict]:
    """Last known value without ever touching the keychain or network."""
    return _cache["data"]
