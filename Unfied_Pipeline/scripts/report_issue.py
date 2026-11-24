"""Record an anomaly for the user to look at. The collecting agent reports problems; it never fixes code.

  python scripts/report_issue.py --kind zero_rows --summary "3 Delisting Committee orders gave 0 rows" \
      --evidence data/raw/master_lists/18fea6d29ab81379.pdf --evidence data/raw/master_lists/2386be42adcabb23.pdf
  python scripts/report_issue.py --list          # open issues (check before reporting, to avoid duplicates)
  python scripts/report_issue.py --close 3 --note "fixed in <commit>"   # for the user, not the agent

Kinds: zero_rows (relevant documents produced no records), bad_rows (junk or wrong records in the master),
audit_error, fetch (blocked/failed domains), script_error (a script crashed or misbehaved),
coverage_gap (year × type cells still empty after their phase), other.

Issues go to logs/issues.jsonl (one JSON object per issue). Reporting the same kind + summary again
bumps its `times_seen` instead of adding a duplicate.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import sys
from contextlib import contextmanager

from common import atomic_write_text, LOGS, agent_session, emit, log, now_iso, read_jsonl

ISSUES = LOGS / "issues.jsonl"
KINDS = ("zero_rows", "bad_rows", "audit_error", "fetch", "script_error", "coverage_gap", "other")


def load() -> list[dict]:
    return read_jsonl(ISSUES) if ISSUES.exists() else []


def save(issues: list[dict]) -> None:
    ISSUES.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(ISSUES, "".join(json.dumps(i, ensure_ascii=False) + "\n" for i in issues))


@contextmanager
def locked():
    """Serialise read-modify-write: the agent may run several report_issue.py calls at once."""
    ISSUES.parent.mkdir(parents=True, exist_ok=True)
    with (LOGS / ".issues.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def report(kind: str, summary: str, evidence: list[str]) -> dict:
    with locked():
        return _report(kind, summary, evidence)


def _report(kind: str, summary: str, evidence: list[str]) -> dict:
    issues = load()
    for i in issues:
        if i["status"] == "open" and i["kind"] == kind and i["summary"] == summary:
            i["times_seen"] += 1
            i["last_seen"] = now_iso()
            i["evidence"] = sorted(set(i["evidence"]) | set(evidence))[:20]
            save(issues)
            return {"reported": False, "duplicate_of": i["id"], "times_seen": i["times_seen"]}
    issue = {"id": len(issues) + 1, "kind": kind, "summary": summary, "evidence": evidence[:20],
             "status": "open", "times_seen": 1, "first_seen": now_iso(), "last_seen": now_iso(),
             "agent_session": agent_session()}
    issues.append(issue)
    save(issues)
    log(f"issue #{issue['id']} [{kind}]: {summary}", level="WARN", kind="issue_reported",
        issue_id=issue["id"], issue_kind=kind, evidence=evidence[:20])
    return {"reported": True, "id": issue["id"]}


def close(issue_id: int, note: str) -> dict:
    with locked():
        issues = load()
        for i in issues:
            if i["id"] == issue_id:
                i.update(status="closed", closed_at=now_iso(), resolution=note)
                save(issues)
                return {"closed": issue_id}
    return {"closed": None, "reason": f"no issue #{issue_id}"}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter,
                                 allow_abbrev=False)
    ap.add_argument("--kind", choices=KINDS)
    ap.add_argument("--summary", help="one sentence: what is wrong, with counts")
    ap.add_argument("--evidence", action="append", default=[], help="artifact path, URL or record_id (repeatable)")
    ap.add_argument("--list", action="store_true", help="print open issues")
    ap.add_argument("--close", type=int, metavar="ID", help="mark an issue resolved (user only)")
    ap.add_argument("--note", default="", help="resolution note for --close")
    a = ap.parse_args()
    if a.close:
        emit(close(a.close, a.note))
        return 0
    if a.list:
        emit({"open": [{k: i[k] for k in ("id", "kind", "summary", "times_seen")}
                       for i in load() if i["status"] == "open"]})
        return 0
    if not (a.kind and a.summary):
        ap.error("--kind and --summary are required (or use --list)")
    emit(report(a.kind, a.summary.strip(), a.evidence))
    return 0


if __name__ == "__main__":
    sys.exit(main())
