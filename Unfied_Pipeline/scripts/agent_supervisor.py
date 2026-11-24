"""What scripts/run_agent.sh does after a Cline session ends, decided from structured state only:

- Cline's exit status (passed in);
- Cline's own `run_result` event: a top-level JSON line of the session log with "type": "run_result", read for
  its `finishReason` field only (never its `text`, which is the model's words);
- the pipeline's run summaries (logs/runs/<run_id>.json, written by collect.py) tagged with this agent session:
  their `status` and `stop_reason` fields.

Nothing the model writes can change the decision: a model that prints "search_engines_suspended" or
"low_disk_space" in its answer does not cause a cooldown or a stop.

  python scripts/agent_supervisor.py decide --session agent-20261123-002818 --log logs/agent_sessions/X.jsonl \
      --exit-code 0 --attempt 0 --max-restarts 5 --cooldowns 0 --max-cooldowns 12

prints one JSON object: {"action": "stop" | "restart" | "cooldown", "reason": "...", "wait_min": N}
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

from common import ROOT, RUNS

STOP_REASONS_THAT_END_THE_LOOP = {"low_disk_space": "low disk space", "network_unavailable": "network unavailable"}


def cline_events(log: Path, kind: str) -> list[dict]:
    """Top-level Cline events of one type. Lines that are not JSON objects are skipped (never searched as text)."""
    out = []
    try:
        lines = log.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return out
    for line in lines:
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if isinstance(ev, dict) and ev.get("type") == kind:
            out.append(ev)
    return out


def finish_reason(log: Path) -> str | None:
    """finishReason of Cline's last run_result event (Cline's own field, not model text)."""
    res = cline_events(log, "run_result")
    fr = res[-1].get("finishReason") if res else None
    return fr if isinstance(fr, str) else None


def last_cline_error(log: Path) -> str | None:
    errs = cline_events(log, "error")
    msg = errs[-1].get("message") if errs else None
    return str(msg)[:200] if msg else None


def session_runs(session: str, runs_dir: Path = RUNS) -> list[dict]:
    """collect.py run summaries written during this agent session, oldest first."""
    out = []
    for p in sorted(runs_dir.glob("*.json")) if runs_dir.is_dir() else []:
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(d, dict) and d.get("agent_session") == session:
            out.append(d)
    return sorted(out, key=lambda d: str(d.get("started_at") or ""))


def search_cooldown_min() -> int:
    """Minutes until the first search engine is usable again (collect.py --dry-run), 30 if unknown."""
    try:
        r = subprocess.run([sys.executable, str(ROOT / "scripts" / "collect.py"), "--dry-run"], capture_output=True,
                           text=True, timeout=120, cwd=ROOT)
        return int(json.loads(r.stdout)["search_cooldown_min_left"])
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
        return 30


def decide(session: str, log: Path, exit_code: int, attempt: int, max_restarts: int, cooldowns: int,
           max_cooldowns: int, interrupted: bool = False, runs_dir: Path = RUNS, cooldown_min=search_cooldown_min) -> dict:
    if interrupted:
        return {"action": "stop", "reason": "interrupted", "wait_min": 0}
    runs = session_runs(session, runs_dir)
    last = runs[-1] if runs else {}
    stop_reason = last.get("stop_reason")
    if stop_reason in STOP_REASONS_THAT_END_THE_LOOP:
        return {"action": "stop", "reason": stop_reason, "wait_min": 0}
    if stop_reason == "search_engines_suspended":
        if cooldowns < max_cooldowns:
            mins = cooldown_min()
            return {"action": "cooldown", "reason": "search_engines_suspended",
                    "wait_min": max(5, min(720, mins + 1))}
        return {"action": "stop", "reason": "search_engines_suspended (cooldown limit reached)", "wait_min": 0}
    finish = finish_reason(log)
    if exit_code == 0 and finish == "completed":
        return {"action": "stop", "reason": "completed", "wait_min": 0}
    why = f"exit {exit_code}" + (f", finish: {finish}" if finish else ", no run_result")
    err = last_cline_error(log)
    if err:
        why += f", cline error: {err}"
    if attempt + 1 > max_restarts:
        return {"action": "stop", "reason": f"gave up after {attempt + 1} abnormal sessions ({why})", "wait_min": 0}
    return {"action": "restart", "reason": why, "wait_min": 0}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter,
                                 allow_abbrev=False)
    sub = ap.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("decide", allow_abbrev=False)
    d.add_argument("--session", required=True)
    d.add_argument("--log", type=Path, required=True)
    d.add_argument("--exit-code", type=int, required=True)
    d.add_argument("--attempt", type=int, default=0)
    d.add_argument("--max-restarts", type=int, default=5)
    d.add_argument("--cooldowns", type=int, default=0)
    d.add_argument("--max-cooldowns", type=int, default=12)
    d.add_argument("--interrupted", action="store_true")
    a = ap.parse_args()
    log = a.log if a.log.is_absolute() else ROOT / a.log
    print(json.dumps(decide(a.session, log, a.exit_code, a.attempt, a.max_restarts, a.cooldowns, a.max_cooldowns,
                            a.interrupted)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
