"""Shared paths, logging, and helpers for the NSE delisting pipeline.

CLI: `python scripts/common.py log "message"` appends a milestone to logs/agent_run.log
(and a structured `agent_note` event to logs/events.jsonl).
"""
from __future__ import annotations

import hashlib
import json
import os
import random
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
SEARCH_CACHE = DATA / "search_cache"
RAW = DATA / "raw"
QUARANTINE = DATA / "quarantine"
PROCESSED = DATA / "processed"
PARSED = PROCESSED / "parsed"
LOGS = ROOT / "logs"
MANIFEST = LOGS / "fetch_manifest.jsonl"
AGENT_LOG = LOGS / "agent_run.log"
EVENTS = LOGS / "events.jsonl"
RUNS = LOGS / "runs"
AGENT_SESSIONS = LOGS / "agent_sessions"
CURRENT_SESSION = AGENT_SESSIONS / "current"  # written by scripts/run_agent.sh while a session runs
METADATA = DATA / "metadata"
CONFIG = ROOT / "config"

SEARXNG_URL = "http://localhost:8082"
PRIORITY_YEARS = {2007, 2017, 2020, 2024}
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0 Safari/537.36 nse-delisting-research/1.0"
)

PRIMARY_DOMAINS = ("nseindia.com", "sebi.gov.in", "bseindia.com")
SECONDARY_DOMAINS = (
    "business-standard.com", "economictimes.indiatimes.com", "moneycontrol.com",
    "livemint.com", "thehindubusinessline.com", "financialexpress.com",
    "ibbi.gov.in", "nclt.gov.in", "taxguru.in", "indiankanoon.org",
)

# Procedural documents (forms, checklists, SOPs, FAQs) describe the delisting process, not delisted companies.
PROCEDURAL_RE = re.compile(r"checklist|\bsop\b|sop_for|annexure|format|faq|guidance|application.?form|"
                           r"template|undertaking|declaration|public.?notice", re.I)

# NSE/BSE pages about trading members, brokers, arbitration etc. list firms, not delisted companies.
NON_LISTING_RE = re.compile(r"member|broker|defaulter|expelled|arbitrat|complaint|debarred|mcsgfc|"
                            r"authori[sz]ed.?person|sub.?broker|depositor|(?<![a-z])ncds?(?![a-z])|debenture", re.I)

SCHEMA_FIELDS = [
    "record_id", "company_name", "nse_symbol", "isin", "delisting_type",
    "order_date", "effective_delisting_date", "calendar_year", "financial_year",
    "is_priority_year", "regulatory_regime", "exit_or_fair_value_inr",
    "source_url", "local_artifact_path", "confidence_status",
]


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def run_id() -> str:
    """Shared id for one collection run. collect.py sets NSE_RUN_ID; standalone scripts get their own."""
    rid = os.environ.get("NSE_RUN_ID")
    if not rid:
        rid = datetime.now().strftime("%Y%m%d-%H%M%S") + f"-{os.getpid()}"
        os.environ["NSE_RUN_ID"] = rid
    return rid


def event(kind: str, script: str | None = None, **fields) -> None:
    """Append one machine-readable event to logs/events.jsonl."""
    rec = {"ts": now_iso(), "run_id": run_id(), "script": script or Path(sys.argv[0]).stem,
           "event": kind, **fields}
    session = agent_session()
    if session:
        rec["agent_session"] = session
    append_jsonl(EVENTS, rec)


def agent_session() -> str | None:
    """The running agent session. Read from the file run_agent.sh writes, because Cline runs commands
    under its long-lived hub process, whose environment still holds the first session's name."""
    try:
        return CURRENT_SESSION.read_text(encoding="utf-8").strip() or None
    except OSError:
        return os.environ.get("NSE_AGENT_SESSION")


def log(msg: str, level: str = "INFO", script: str | None = None, kind: str = "log", **fields) -> None:
    """Human-readable line in logs/agent_run.log plus a structured event in logs/events.jsonl."""
    AGENT_LOG.parent.mkdir(parents=True, exist_ok=True)
    name = script or Path(sys.argv[0]).stem
    with AGENT_LOG.open("a", encoding="utf-8") as f:
        f.write(f"{now_iso()} [{level}] [{run_id()}] {name}: {msg}\n")
    event(kind, script=name, level=level, msg=msg, **fields)


def isin_valid(code: str) -> bool:
    """Indian equity ISIN (INE…, or IN9… for partly paid) with a valid Luhn check digit. SEBI
    registration numbers (INM merchant banker, INR registrar, INZ broker) look similar but fail."""
    if not re.fullmatch(r"IN[E9][A-Z0-9]{8}[0-9]", code or ""):
        return False
    digits = "".join(str(int(c, 36)) for c in code)
    total = 0
    for i, d in enumerate(reversed(digits)):
        n = int(d) * (2 if i % 2 else 1)
        total += n - 9 if n > 9 else n
    return total % 10 == 0


# ISIN characters 8-9 are the security type: 07/08 debentures and bonds, 14 commercial paper,
# 16 certificates of deposit. Those are debt, not the company's shares being delisted.
DEBT_SECURITY_TYPES = {"07", "08", "14", "16"}


def isin_equity(code: str) -> bool:
    return isin_valid(code) and code[7:9] not in DEBT_SECURITY_TYPES


def sha256_text(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def polite_sleep(lo: float = 2.0, hi: float = 5.0) -> None:
    time.sleep(random.uniform(lo, hi))


def domain_of(url: str) -> str:
    host = (urlparse(url).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def domain_matches(url: str, domains) -> bool:
    d = domain_of(url)
    return any(d == x or d.endswith("." + x) for x in domains)


def confidence_for_url(url: str) -> str:
    if domain_matches(url, PRIMARY_DOMAINS):
        return "VERIFIED_PRIMARY"
    if domain_matches(url, SECONDARY_DOMAINS):
        return "CORROBORATED_SECONDARY"
    return "NEEDS_REVIEW"


def raw_dir_for_tag(year_tag: str | None) -> Path:
    tag = (year_tag or "").strip().lower()
    if tag == "master":
        return RAW / "master_lists"
    if tag.isdigit() and int(tag) in PRIORITY_YEARS:
        return RAW / f"priority_{tag}"
    return RAW / "archive_2006_2026"


def atomic_write_text(path: Path, text: str) -> None:
    """Write a whole file so readers see the old or the new content, never a partial one: a temp file in the
    same directory, fsync, then os.replace. On failure the old file is untouched and the temp file removed."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with tmp.open("w", encoding="utf-8", newline="") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def cleanup_partials(dirs, older_than_s: float = 3600) -> int:
    """Remove leftovers of interrupted writes (.<name>.part downloads, .<name>.<pid>.tmp atomic writes) older than
    older_than_s (younger ones may belong to a write in progress). Returns how many were removed."""
    n, cutoff = 0, time.time() - older_than_s
    for d in dirs:
        d = Path(d)
        if not d.exists():
            continue
        for f in d.rglob(".*"):
            try:
                if f.is_file() and f.name.endswith((".part", ".tmp")) and f.stat().st_mtime < cutoff:
                    f.unlink()
                    n += 1
            except OSError:
                pass
    return n


def append_jsonl(path: Path, obj: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False) + "\n")


def read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows = []
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    return rows


DISK_DEFAULTS = {"warn_data_gb": 5.0, "warn_step_gb": 5.0, "min_free_gb": 20.0}


def disk_settings() -> dict:
    """Thresholds from the "disk" section of config/query_plan.json (falls back to defaults)."""
    try:
        cfg = json.loads((CONFIG / "query_plan.json").read_text(encoding="utf-8")).get("disk", {})
    except (OSError, json.JSONDecodeError):
        cfg = {}
    out = dict(DISK_DEFAULTS)
    for k, v in (cfg.items() if isinstance(cfg, dict) else []):
        if k in DISK_DEFAULTS:
            try:
                out[k] = float(v)
            except (TypeError, ValueError):  # keep the safe default; the bad value is reported, never fatal
                print(f"warning: config disk.{k}={v!r} is not a number; using {DISK_DEFAULTS[k]}", file=sys.stderr)
    return out


def dir_size(path: Path) -> int:
    return sum(p.stat().st_size for p in path.rglob("*") if p.is_file())


def disk_status() -> dict:
    """Size of data/ and free space on its volume, with a level: ok | warn | critical.
    `warn_tier` increases by one for every `warn_step_gb` beyond `warn_data_gb`."""
    s = disk_settings()
    data_gb = dir_size(DATA) / 1e9
    free_gb = shutil.disk_usage(ROOT).free / 1e9
    tier = int((data_gb - s["warn_data_gb"]) // s["warn_step_gb"]) + 1 if data_gb >= s["warn_data_gb"] else 0
    level = "critical" if free_gb < s["min_free_gb"] else "warn" if tier else "ok"
    return {"data_gb": round(data_gb, 2), "free_gb": round(free_gb, 1), "level": level,
            "warn_tier": tier, **s}


def notify(title: str, message: str) -> None:
    """Best-effort macOS notification (no-op elsewhere)."""
    if sys.platform == "darwin":
        script = f"display notification {json.dumps(message)} with title {json.dumps(title)} sound name \"Glass\""
        subprocess.run(["osascript", "-e", script], capture_output=True, timeout=10)


def check_disk(state: dict) -> dict:
    """Warn (log + terminal + notification) once per new warn tier; `state` remembers the last tier.
    Returns disk_status(); callers stop the run when level == "critical"."""
    d = disk_status()
    last = state.get("disk_warn_tier", 0)
    if d["level"] == "critical":
        msg = f"Free disk space {d['free_gb']} GB < {d['min_free_gb']} GB — stopping. data/ is {d['data_gb']} GB."
        print(f"!! DISK CRITICAL: {msg}", flush=True)
        log(msg, level="ERROR", kind="disk_critical", disk=d)
        notify("NSE collection stopped: low disk", msg)
    elif d["warn_tier"] > last:
        msg = (f"data/ has grown to {d['data_gb']} GB (warning threshold {d['warn_data_gb']} GB, "
               f"free {d['free_gb']} GB). Free up space or raise disk.warn_data_gb in config/query_plan.json.")
        print(f"!! DISK WARNING: {msg}", flush=True)
        log(msg, level="WARN", kind="disk_warning", disk=d)
        notify("NSE collection: data folder is large", msg)
    state["disk_warn_tier"] = max(last, d["warn_tier"])
    return d


def graceful_sigterm() -> None:
    """Treat SIGTERM (timeout, kill, job.py stop) like Ctrl-C, so runs save their state and summary."""
    import signal

    def handler(signum, frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, handler)


def emit(summary: dict) -> None:
    """Print a compact JSON summary for the agent."""
    print(json.dumps(summary, ensure_ascii=False, default=str))


if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "log":
        log(" ".join(sys.argv[2:]), script="agent", kind="agent_note")
        emit({"logged": True})
    else:
        print('usage: python scripts/common.py log "message"')
        sys.exit(2)
