"""Triage classifier: inspect each session's transcript to determine its state."""
from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Optional

IDLE_THRESHOLD = 300     # 5 min
CLOSEABLE_THRESHOLD = 3600  # 1 hour

TRIAGE_PRIORITY = {
    "waiting_perm": 0,
    "stalled": 1,
    "completed": 2,
    "working": 3,
    "closeable": 4,
}


_BG_KEYWORDS = re.compile(r"等待|等.*通知|后台|background|polling|monitor|run_in_background", re.IGNORECASE)


def _last_assistant_info(transcript_path: str) -> Optional[dict]:
    """Extract stop_reason, last content block type, and background task status."""
    p = Path(transcript_path)
    if not p.exists():
        return None
    lines: list[str] = []
    try:
        with p.open() as f:
            for line in f:
                lines.append(line)
    except Exception:
        return None

    # Check for active background tasks: only queue-operations AFTER the
    # last assistant end_turn count. If the session moved on past the bg
    # task phase, stale queue-ops don't indicate active work.
    has_pending_background = False
    last_end_turn_idx = -1
    tail = lines[-30:]
    for i, raw in enumerate(tail):
        try:
            d = json.loads(raw)
        except Exception:
            continue
        t = d.get("type", "")
        if t == "assistant" and (d.get("message") or {}).get("stop_reason") == "end_turn":
            last_end_turn_idx = i
            has_pending_background = False
        elif t == "queue-operation" and i > last_end_turn_idx:
            has_pending_background = True

    # Find the last assistant message for stop_reason etc.
    stop_reason = ""
    last_block_type = ""
    last_text = ""
    last_tool = ""
    for raw in reversed(lines[-40:]):
        try:
            d = json.loads(raw)
        except Exception:
            continue
        if d.get("type") != "assistant":
            continue
        msg = d.get("message") or {}
        content = msg.get("content") or []
        stop_reason = msg.get("stop_reason", "")
        if isinstance(content, list) and content:
            last_block = content[-1]
            last_block_type = last_block.get("type", "")
            if last_block_type == "text":
                last_text = last_block.get("text", "")
            elif last_block_type == "tool_use":
                last_tool = last_block.get("name", "")
            for c in reversed(content):
                if c.get("type") == "text" and c.get("text", "").strip():
                    last_text = c["text"].strip()
                    break
        break

    # Keyword fallback: if the assistant's last text mentions waiting for background work
    if not has_pending_background and last_text and _BG_KEYWORDS.search(last_text):
        has_pending_background = True

    return {
        "stop_reason": stop_reason,
        "last_block_type": last_block_type,
        "last_text": last_text[:200],
        "last_tool": last_tool,
        "has_pending_background": has_pending_background,
    }


def classify(window_dict: dict) -> dict:
    """Classify a window dict (from sessions.snapshot) into a triage state.

    Returns {triage, reason, suggestion}.
    """
    status = window_dict.get("status", "unknown")
    idle = window_dict.get("idle_seconds", 0)
    name = window_dict.get("name") or window_dict.get("project_name") or ""
    transcript = window_dict.get("transcript_path")

    if status == "waiting":
        return {
            "triage": "waiting_perm",
            "reason": window_dict.get("waiting_for") or "等待授权",
            "suggestion": "去终端批准",
        }

    # Session is running a Codex review (exec child process or in-flight MCP
    # call). Stalled = exec rollout silent too long (likely hung).
    cr = window_dict.get("codex_review")
    if cr:
        elapsed = _format_idle(cr.get("elapsed_s") or 0)
        if cr.get("stalled"):
            silent = _format_idle(cr.get("silent_s") or 0)
            return {
                "triage": "stalled",
                "reason": f"Codex 审查疑似卡死：已 {elapsed}，输出停滞 {silent}",
                "suggestion": "查看 Codex 进度，考虑重启该审查",
            }
        act = (cr.get("current_action") or "")[:70]
        tail = f"。{act}" if act else ("（MCP 调用，无实时输出）" if cr.get("source") == "mcp" else "")
        return {
            "triage": "working",
            "reason": f"Codex 审查中 · 已 {elapsed}{tail}",
            "suggestion": "",
        }

    # A live Workflow run: multi-agent fan-out executing in the background.
    # The main turn usually ends right after launching it, so without this
    # the card would read "completed" while dozens of agents still work.
    wf = window_dict.get("workflow_run")
    if wf:
        name = wf.get("name") or "workflow"
        elapsed = _format_idle(wf.get("elapsed_s") or 0)
        prog = ""
        if wf.get("agents_started"):
            prog = f"，agents {wf.get('agents_done') or 0}/{wf['agents_started']} 完成"
        if wf.get("stalled"):
            silent = _format_idle(wf.get("silent_s") or 0)
            return {
                "triage": "stalled",
                "reason": f"Workflow {name} 疑似卡死：已 {elapsed}{prog}，无新输出 {silent}",
                "suggestion": "用 /workflows 查看进度，必要时 TaskStop 后 resume",
            }
        return {
            "triage": "working",
            "reason": f"Workflow {name} 运行中 · 已 {elapsed}{prog}",
            "suggestion": "",
        }

    # Session is waiting on GPU work: either sleeping on a ScheduleWakeup
    # (has a concrete wake time) or running a background waiter (poll loop).
    # Without this it would read as generic "working" / "stalled 停在 ScheduleWakeup".
    pw = window_dict.get("pending_wakeup")
    if pw:
        label = "等 GPU" if pw.get("kind") == "gpu" else "等待定时唤醒"
        why = (pw.get("reason") or "").split("\n")[0][:80]
        wake_ms = pw.get("wake_at_ms")
        if wake_ms:
            wake_hhmm = time.strftime("%H:%M", time.localtime(wake_ms / 1000))
            if pw.get("overdue"):
                return {
                    "triage": "stalled",
                    "reason": f"{label}，唤醒已过期（原定 {wake_hhmm}）。{why}",
                    "suggestion": "检查会话是否卡住",
                }
            return {
                "triage": "working",
                "reason": f"{label} · 下次唤醒 {wake_hhmm}。{why}",
                "suggestion": "",
            }
        itv = pw.get("poll_interval_s")
        if itv and itv >= 60:
            cadence = f"后台 waiter 每 ~{itv // 60}m 检查"
        elif itv:
            cadence = f"后台 waiter 每 {itv}s 检查"
        else:
            cadence = "后台 waiter 监控中"
        return {
            "triage": "working",
            "reason": f"{label}（{cadence}）。{why}",
            "suggestion": "",
        }

    if status == "busy" and idle < IDLE_THRESHOLD:
        return {
            "triage": "working",
            "reason": "正在工作",
            "suggestion": "",
        }

    if status == "shell":
        return {
            "triage": "working",
            "reason": "shell 进程运行中",
            "suggestion": "",
        }

    if not transcript:
        return {
            "triage": "closeable",
            "reason": "无 transcript 记录",
            "suggestion": "可以关闭",
        }

    info = _last_assistant_info(transcript)
    if not info:
        return {
            "triage": "closeable",
            "reason": "transcript 为空",
            "suggestion": "可以关闭",
        }

    stop = info["stop_reason"]
    idle_str = _format_idle(idle)

    if info.get("has_pending_background"):
        summary = info["last_text"].split("\n")[0][:80] if info["last_text"] else ""
        return {
            "triage": "working",
            "reason": f"有后台任务在执行。{summary}",
            "suggestion": "",
        }

    if stop == "end_turn":
        summary = info["last_text"].split("\n")[0][:80] if info["last_text"] else ""
        if idle >= CLOSEABLE_THRESHOLD:
            return {
                "triage": "closeable",
                "reason": f"已完成，空闲 {idle_str}。{summary}",
                "suggestion": "可以关闭",
            }
        return {
            "triage": "completed",
            "reason": f"已完成，空闲 {idle_str}。{summary}",
            "suggestion": "建议 review",
        }

    if stop == "tool_use":
        tool = info["last_tool"]
        if status == "busy":
            return {
                "triage": "working",
                "reason": f"正在执行 {tool}" if tool else "正在工作",
                "suggestion": "",
            }
        return {
            "triage": "stalled",
            "reason": f"停在 {tool}，空闲 {idle_str}" if tool else f"中途停止，空闲 {idle_str}",
            "suggestion": "需要用户介入",
        }

    # Fallback
    if idle >= CLOSEABLE_THRESHOLD:
        return {
            "triage": "closeable",
            "reason": f"空闲 {idle_str}",
            "suggestion": "可以关闭",
        }
    return {
        "triage": "completed" if idle >= IDLE_THRESHOLD else "working",
        "reason": f"空闲 {idle_str}",
        "suggestion": "",
    }


def _format_idle(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    h = seconds // 3600
    m = (seconds % 3600) // 60
    return f"{h}h{m}m" if m else f"{h}h"
