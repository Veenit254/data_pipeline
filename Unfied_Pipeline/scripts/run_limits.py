"""Run limits (caps) and run modes for collect.py, and the stop file the stages share.

config/query_plan.json:
  "run_limits": {"mode": "capped", "max_files": 60, "max_companies": 25, "max_docs_per_company": 5}
  mode      capped     use the caps below
            unlimited  no caps: run until the queries, the docket or the sources are used up
            ask        ask at the start of a run, but only on an interactive terminal; unattended runs
                       (the agent, job.py, cron) use "capped"
  max_files             files downloaded OK in this run          -> stop reason max_files_reached
  max_companies         new records in the master in this run    -> max_companies_reached
  max_docs_per_company  documents fetched per company (all time) -> further ones stay parked
  0, null or "all" = no limit for that cap.
collect.py flags override the config: --max-files N|all, --max-companies N|all,
--max-docs-per-company N|all, --unlimited, --capped.

The stop file logs/runs/.<run_id>.stop holds the reason; whichever stage reaches a cap writes it and the
other stages stop at their next step.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from common import CONFIG

CAPS = ("max_files", "max_companies", "max_docs_per_company")
DEFAULTS = {"mode": "capped", "max_files": 60, "max_companies": 25, "max_docs_per_company": 5}
MODES = ("capped", "unlimited", "ask")
# stop reasons, for documentation and tests
STOP_REASONS = ("max_files_reached", "max_companies_reached", "max_queries_reached", "queries_exhausted",
                "docket_exhausted", "sources_exhausted")


def parse_cap(v) -> int | None:
    """None = no limit. Accepts ints, numeric strings, 0, null, "all", "none", "unlimited"."""
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, str):
        s = v.strip().lower()
        if s in ("", "all", "none", "null", "unlimited", "0"):
            return None
        if not s.isdigit():
            raise ValueError(f"not a limit: {v!r} (use a number, 0, null or all)")
        v = int(s)
    v = int(v)
    if v < 0:
        raise ValueError(f"limits can't be negative: {v}")
    return v or None


def from_config() -> dict:
    try:
        cfg = json.loads((CONFIG / "query_plan.json").read_text(encoding="utf-8")).get("run_limits") or {}
    except (OSError, ValueError):
        cfg = {}
    return {**DEFAULTS, **{k: v for k, v in cfg.items() if not k.startswith("_")}}


def ask_mode(caps: dict, ask=input, out=sys.stderr) -> str:
    out.write("\nRun limits for this run:\n"
              f"  1) Capped: {describe(caps)} (recommended)\n"
              "  2) Unlimited: keep going until the queries, the docket or the sources are used up\n")
    return "unlimited" if ask("  > [1] ").strip() == "2" else "capped"


def resolve(args, interactive: bool, ask=input) -> dict:
    """The limits for this run: config, overridden by flags; 'ask' only prompts on a terminal."""
    cfg = from_config()
    mode = cfg.get("mode", "capped")
    if mode not in MODES:
        raise ValueError(f"run_limits.mode must be one of {MODES}, not {mode!r}")
    flag_caps = {k: getattr(args, k, None) for k in CAPS}
    if getattr(args, "unlimited", False):
        mode = "unlimited"
    elif getattr(args, "capped", False) or any(v is not None for v in flag_caps.values()):
        mode = "capped"
    caps = {k: parse_cap(cfg.get(k)) for k in CAPS}
    if mode == "ask":
        mode = ask_mode(caps, ask) if interactive else "capped"
    if mode == "unlimited":
        caps = {k: None for k in CAPS}
    for k, v in flag_caps.items():
        if v is not None:
            caps[k] = parse_cap(v)
    return {"mode": mode, **caps}


def describe(lim: dict) -> str:
    def one(k, label):
        return f"{lim[k]} {label}" if lim.get(k) else f"no {label} cap"
    return ", ".join([one("max_files", "files"), one("max_companies", "companies"),
                      one("max_docs_per_company", "docs/company")])


class StopFile:
    """A run-wide stop signal: the first stage to hit a cap writes the reason, the others read it."""

    def __init__(self, path: Path | str | None):
        self.path = Path(path) if path else None

    def reason(self) -> str | None:
        if not self.path:
            return None
        try:
            return self.path.read_text(encoding="utf-8").strip() or None
        except OSError:
            return None

    def stop(self, reason: str) -> None:
        if self.path and not self.reason():
            self.path.write_text(reason, encoding="utf-8")
