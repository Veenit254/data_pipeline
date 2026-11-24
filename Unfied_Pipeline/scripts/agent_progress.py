"""Turn Cline's `--json` event stream (stdin) into short human-readable progress lines.

Used by scripts/run_agent.sh; the raw stream is saved separately to logs/agent_sessions/.
Also appends milestone lines (tool calls, completion) to logs/agent_run.log.
"""
from __future__ import annotations

import json
import sys

from common import log

SKIP_TOOLS = {"submit_and_exit"}


def short(v, n: int = 110) -> str:
    s = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)
    s = " ".join(s.split())
    return s if len(s) <= n else s[: n - 1] + "…"


def main() -> int:
    tools = 0
    for line in sys.stdin:
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            continue
        ev = e.get("event") or {}
        t = ev.get("type")
        if t == "content_start" and ev.get("contentType") == "tool":
            tools += 1
            name, inp = ev.get("toolName", "?"), ev.get("input") or {}
            detail = inp.get("command") or inp.get("path") or inp.get("query") or inp
            if name not in SKIP_TOOLS:
                print(f"  ▶ [{tools}] {name}: {short(detail)}", flush=True)
                log(f"tool {name}: {short(detail, 200)}", script="cline", kind="agent_tool", tool=name)
        elif t == "content_end" and ev.get("contentType") == "text" and ev.get("text", "").strip():
            print(f"  💬 {short(ev['text'], 160)}", flush=True)
        elif e.get("type") == "run_result":
            u = e.get("aggregateUsage") or e.get("usage") or {}
            msg = (f"session finished: {e.get('finishReason')} after {e.get('iterations')} iterations, "
                   f"{tools} tool calls, {u.get('inputTokens')} in / {u.get('outputTokens')} out tokens")
            print(f"■ {msg}", flush=True)
            log(msg, script="cline", kind="agent_session_end", finish_reason=e.get("finishReason"),
                iterations=e.get("iterations"), tool_calls=tools)
        elif e.get("type") == "error":
            print(f"  ✖ {short(e.get('message'), 200)}", flush=True)
            log(f"agent error: {short(e.get('message'), 300)}", level="ERROR", script="cline", kind="agent_error")
    return 0


if __name__ == "__main__":
    sys.exit(main())
