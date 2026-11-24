"""Permission boundary for the collecting agent's tool calls. Cline runs it before every tool call
(.cline/hooks/PreToolUse.sh) with the call as JSON on stdin.

How it is enforced (an allowlist grammar, not a blacklist of dangerous strings):
1. Exact grammar. A command is parsed into a *plan* (pipelines joined by && / || / ;). Every program and
   every argument must match the grammar below: agent scripts with only the flags the agent needs (and
   values in range), `git status`, `cd <project root>`, and pipe filters (head/tail/wc/grep/sort/uniq/cut)
   that read stdin only: no file operands, no -o/-r/-f/--files0-from, …
2. Nothing for bash to interpret. Outside quotes  $ ` ~ * ? [ ] { } ! \\ #  are refused, inside double
   quotes  $ ` \\ !  ; redirections, background jobs, here-docs, env assignments and control characters are
   refused. So the string bash would see means exactly what the guard checked.
3. No shell at all. An allowed call is *rewritten* into `.venv/bin/python scripts/agent_exec.py <token>`
   (token = the plan, base64). agent_exec.py re-validates the plan with this module and runs the scripts
   with an argv list (no shell); the pipe filters are implemented in Python on the output.
4. No direct edits. editor / apply_patch are refused everywhere; the plan changes only through
   scripts/add_queries.py (append-only, validated). The agent's scripts also refuse abbreviated options
   (argparse allow_abbrev=False), and job.py caps can only be tightened, never loosened.

Everything else (curl, rm, ls, python -c, web fetches, edits, …) needs the user's approval. Nobody approves
an unattended run, so such a call is not executed: it is rewritten into a harmless one that tells the agent it
was blocked, logged (`tool_denied` event) and filed in logs/issues.jsonl. A Cline hook may not simply cancel
a call (cancel ends the whole run), hence the rewrite. Every checked call goes to logs/tool_guard.jsonl.

  echo '{"tool_call": {"name": "run_commands", "input": {"commands": ["curl x"]}}}' | python scripts/tool_guard.py
"""
from __future__ import annotations

import base64
import json
import re
import shlex
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

NOTICE = ROOT / ".cline" / "BLOCKED_READ.txt"
AUDIT = ROOT / "logs" / "tool_guard.jsonl"
PYTHONS = {".venv/bin/python", str(ROOT / ".venv/bin/python")}
RUNNER = ".venv/bin/python scripts/agent_exec.py"
OPS = ("&&", "||", ";")
NO_READ = ("scripts/", "tests/", "vendor/", ".venv/", "node_modules/", "data/raw/", "data/quarantine/",
           "data/search_cache/", "data/processed/parsed/", "data/processed/ocr/", "logs/agent_sessions/",
           ".git/", ".cline/hooks/", "qa/")
MAX_AGENT_QUERIES = 20          # job.py start --max-queries
DOCKET_STATUSES = ("parked", "pending", "fetching", "fetched", "parsed", "failed", "dead", "parse_error")
ISSUE_KINDS = ("zero_rows", "bad_rows", "audit_error", "fetch", "script_error", "coverage_gap", "other")


class Bad(ValueError):
    """The call is outside the grammar; the message says why."""


# ---------- the user's caps (the agent may tighten them, never loosen) --------------------------------------
def user_caps() -> dict:
    try:
        import run_limits
        cfg = run_limits.from_config()
        return {"mode": cfg.get("mode", "capped"),
                **{k: run_limits.parse_cap(cfg.get(k)) for k in run_limits.CAPS}}
    except Exception:  # unreadable config: the safest caps
        return {"mode": "capped", "max_files": 60, "max_companies": 25, "max_docs_per_company": 5}


# ---------- value validators ----------------------------------------------------------------------------------
def _int(lo: int, hi: int):
    def v(x: str):
        if not re.fullmatch(r"\d{1,7}", x) or not lo <= int(x) <= hi:
            raise Bad(f"{x!r} is not a number from {lo} to {hi}")
    return v


def _choice(*xs: str):
    def v(x: str):
        if x not in xs:
            raise Bad(f"{x!r} is not one of {', '.join(xs)}")
    return v


def _text(n: int):
    def v(x: str):
        if not x.strip() or len(x) > n or any(ord(ch) < 32 or ord(ch) == 127 for ch in x):
            raise Bad(f"text must be 1-{n} printable characters")
    return v


def _name(x: str):
    if not re.fullmatch(r"[a-z0-9_]{1,60}", x):
        raise Bad(f"{x!r} is not a phase name")


def _cap(key: str):
    def v(x: str):
        _int(1, 100000)(x)
        caps = user_caps()
        if caps.get("mode") != "unlimited" and caps.get(key) and int(x) > caps[key]:
            raise Bad(f"--{key.replace('_', '-')} {x} is above the user's cap ({caps[key]}); caps are the user's")
    return v


def rel(path: str) -> str | None:
    """Path relative to the project root, or None if it is outside the project."""
    p = Path(path).expanduser()
    p = (p if p.is_absolute() else ROOT / p).resolve()
    return str(p.relative_to(ROOT)) if p.is_relative_to(ROOT) else None


def _data_path(x: str):
    r = rel(x)
    if not r or not r.startswith("data/"):
        raise Bad(f"{x!r} is not a file under data/")


FLAG = None  # spec value for an option without a value


def options(args: list[str], spec: dict, positional=None, max_positional: int = 0) -> dict:
    """Parse args against spec {option: validator | FLAG | (validator, max_count)}: exact option names only
    (no abbreviations), `--opt=value` accepted. Returns {option: [values], "_pos": [positionals]}."""
    out: dict = {"_pos": []}
    i = 0
    while i < len(args):
        a = args[i]
        if a.startswith("-") and a != "-":
            name, eq, val = a.partition("=")
            if name not in spec:
                raise Bad(f"option {name} is not allowed here (allowed: {', '.join(spec) or 'none'})")
            rule = spec[name]
            check, max_n = rule if isinstance(rule, tuple) else (rule, 1)
            if check is FLAG:
                if eq:
                    raise Bad(f"{name} takes no value")
            else:
                if not eq:
                    i += 1
                    if i >= len(args):
                        raise Bad(f"{name} needs a value")
                    val = args[i]
                check(val)
            out.setdefault(name, []).append(val)
            if len(out[name]) > max_n:
                raise Bad(f"{name} at most {max_n} time(s)")
        else:
            if not positional or len(out["_pos"]) >= max_positional:
                raise Bad(f"unexpected argument {a!r}")
            positional(a)
            out["_pos"].append(a)
        i += 1
    return out


# ---------- agent scripts: exact argument grammar --------------------------------------------------------------
def _collect(args):
    o = options(args, {"--dry-run": FLAG, "--phase": _name, "--max-queries": _int(0, 100000),
                       "--stage": _choice("all", "discover", "fetch", "process", "drain")})
    if "--dry-run" not in o:
        raise Bad("collect.py runs only with --dry-run; start runs with scripts/job.py start")


def _job(args):
    if not args:
        raise Bad("job.py takes start|wait|status|stop")
    sub, rest = args[0], args[1:]
    if sub == "start":
        options(rest, {"--max-queries": _int(1, MAX_AGENT_QUERIES), "--harvest-only": FLAG,
                       "--stage": _choice("drain", "discover"), "--phase": _name,
                       "--max-files": _cap("max_files"), "--max-companies": _cap("max_companies"),
                       "--max-docs-per-company": _cap("max_docs_per_company")})
    elif sub == "wait":
        options(rest, {}, _int(1, 100000), 1)
    elif sub in ("status", "stop"):
        options(rest, {})
    else:
        raise Bad("job.py takes start|wait|status|stop")


def _status(args):
    options(args, {"--json": FLAG, "--year": _int(1990, 2030), "--needs-review": FLAG})


def _report(args):
    if args == ["--list"]:
        return
    o = options(args, {"--kind": _choice(*ISSUE_KINDS), "--summary": _text(300), "--evidence": (_text(300), 20)})
    if "--kind" not in o or "--summary" not in o:
        raise Bad("report_issue.py --list, or --kind K --summary S [--evidence E ...]")


def _inspect(args):
    o = options(args, {"--zero-rows": FLAG}, _data_path, 1)
    if bool(o.get("--zero-rows")) == bool(o["_pos"]):
        raise Bad("inspect_artifact.py --zero-rows, or one file under data/")


def _search(args):
    if len(args) != 1 or args[0] not in ("--engines", "--providers", "--check", "--list-cache"):
        raise Bad("web_search.py only with one of --engines, --check, --list-cache; searches go through collect.py")


def _docket(args):
    if args == ["--stats"]:
        return
    o = options(args, {"--list": _choice(*DOCKET_STATUSES), "--limit": _int(1, 100)})
    if "--list" not in o:
        raise Bad("docket.py only with --stats, or --list STATUS [--limit N<=100]")


def _common(args):
    if len(args) != 2 or args[0] != "log":
        raise Bad('common.py only as: common.py log "<message>"')
    _text(500)(args[1])


def _add_queries(args):
    o = options(args, {"--year-tag": _text(10), "--query": (_text(200), 8), "--add-domain": (_text(253), 3)})
    if "--query" not in o and "--add-domain" not in o:
        raise Bad("add_queries.py --year-tag Y --query Q [...] and/or --add-domain D")


def _no_args(args):
    options(args, {})


SCRIPTS = {
    "collect.py": _collect, "job.py": _job, "status_report.py": _status, "report_issue.py": _report,
    "inspect_artifact.py": _inspect, "audit_dataset.py": _no_args, "build_metadata.py": _no_args,
    "web_search.py": _search, "docket.py": _docket, "common.py": _common, "add_queries.py": _add_queries,
}


# ---------- pipe filters: stdin only ---------------------------------------------------------------------------
FILTERS = {"head", "tail", "wc", "grep", "sort", "uniq", "cut"}


def short_flags(args: list[str], letters: str, valued: dict, max_pos: int) -> tuple[dict, list[str]]:
    """Short options only: combined flags (-iv), valued options (-n 5 / -n5), `-<digits>` if "N" is valued.
    Returns ({letter: [values or True]}, positionals). Long options and '-' are refused."""
    seen: dict = {}
    pos: list[str] = []
    i = 0
    while i < len(args):
        a = args[i]
        if a.startswith("--") or a == "-":
            raise Bad(f"option {a} is not allowed in a filter")
        if a.startswith("-"):
            body = a[1:]
            if body.isdigit() and "N" in valued:
                valued["N"](body)
                seen.setdefault("n", []).append(body)
                i += 1
                continue
            for j, ch in enumerate(body):
                if ch in letters:
                    seen.setdefault(ch, []).append(True)
                    continue
                if ch in valued:
                    val = body[j + 1:]
                    if not val:
                        i += 1
                        if i >= len(args):
                            raise Bad(f"-{ch} needs a value")
                        val = args[i]
                    valued[ch](val)
                    seen.setdefault(ch, []).append(val)
                    break
                raise Bad(f"option -{ch} is not allowed in a filter")
        else:
            if len(pos) >= max_pos:
                raise Bad("filters only read piped output, never files")
            pos.append(a)
        i += 1
    return seen, pos


def _single_char(x: str):
    if len(x) != 1:
        raise Bad("delimiter must be one character")


def _cut_list(x: str):
    if not re.fullmatch(r"\d*(?:-\d*)?(?:,\d*(?:-\d*)?)*", x) or not re.search(r"\d", x):
        raise Bad(f"{x!r} is not a cut list")


def _sort_key(x: str):
    if not re.fullmatch(r"\d{1,2}(?:,\d{1,2})?", x):
        raise Bad(f"{x!r} is not a sort key")


def _any(x: str):
    _text(200)(x)


N = _int(0, 100000)
FILTER_SPECS = {  # name: (flag letters, valued letters, positionals allowed)
    "head": ("", {"n": N, "N": N}, 0),
    "tail": ("", {"n": N, "N": N}, 0),
    "wc": ("lcwm", {}, 0),
    "sort": ("nruf", {"k": _sort_key, "t": _single_char}, 0),
    "uniq": ("cdui", {}, 0),
    "cut": ("", {"d": _single_char, "f": _cut_list, "c": _cut_list, "b": _cut_list}, 0),
    "grep": ("ivcEFwxon", {"e": _any, "m": N, "A": N, "B": N, "C": N}, 1),
}


def check_filter(words: list[str]) -> tuple[dict, list[str]]:
    name, args = words[0], words[1:]
    if name not in FILTER_SPECS:
        raise Bad(f"only {', '.join(sorted(FILTERS))} may follow a pipe")
    letters, valued, max_pos = FILTER_SPECS[name]
    seen, pos = short_flags(args, letters, valued, max_pos)
    if name == "grep":
        for p in pos:
            _any(p)
        if len(seen.get("e", [])) + len(pos) != 1:
            raise Bad("grep takes exactly one pattern and reads piped output only")
    if name == "cut" and sum(k in seen for k in "fcb") != 1:
        raise Bad("cut needs exactly one of -f, -c, -b")
    return seen, pos


# ---------- plans: parse a command string, validate a plan ------------------------------------------------------
UNQUOTED_BAD = set("$`~*?[]{}!\\#<>()")
DQUOTED_BAD = set("$`\\!")


def scan(cmd: str) -> str:
    """Refuse anything bash would expand or interpret beyond words, quotes and && || ; |.
    Unquoted newlines become ';'. Returns the command ready for shlex."""
    cmd = re.sub(r"(?:(?<=\s)|^)2>&1(?=\s|$)", " ", cmd)  # stderr merge is harmless (the runner merges anyway)
    out, state = [], None
    for ch in cmd:
        if ch == "\n" and state is None:
            out.append(" ; ")
            continue
        if ord(ch) < 32 and ch != "\t" or ord(ch) == 127:
            raise Bad("control characters are not allowed")
        if state is None:
            if ch in UNQUOTED_BAD:
                raise Bad(f"'{ch}' is not allowed outside quotes (no shell expansion, redirection or subshell)")
            if ch in "'\"":
                state = ch
        elif state == '"':
            if ch in DQUOTED_BAD:
                raise Bad(f"'{ch}' is not allowed inside double quotes (no shell expansion)")
            if ch == '"':
                state = None
        elif ch == "'":
            state = None
        out.append(ch)
    if state:
        raise Bad("unterminated quote")
    return "".join(out)


def parse(cmd: str) -> list[dict]:
    """Command string -> plan: [{"op": None | "&&" | "||" | ";", "pipeline": [argv, ...]}] (normalized)."""
    text = scan(cmd)
    try:
        lex = shlex.shlex(text, posix=True, punctuation_chars=True)
        lex.whitespace_split = True
        lex.commenters = ""
        tokens = list(lex)
    except ValueError as e:
        raise Bad(f"cannot parse command: {e}")
    while tokens and tokens[-1] == ";":
        tokens.pop()
    plan, op, pipeline, cur = [], None, [], []
    for t in tokens + [None]:
        if t is None or t in ("|",) + OPS:
            if not cur:
                raise Bad("empty command segment")
            pipeline.append(cur)
            cur = []
            if t != "|":
                plan.append({"op": op, "pipeline": pipeline})
                op, pipeline = t, []
        elif t and set(t) <= set("&|;<>()"):
            raise Bad(f"'{t}' (redirection/background) is not allowed")
        else:
            cur.append(t)
    return [normalize_pipeline(p) for p in plan]


def normalize_pipeline(p: dict) -> dict:
    """First stage: python scripts/X.py -> scripts/X.py; `cd <root>` -> cd."""
    first, *rest = p["pipeline"]
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", first[0]):
        raise Bad("environment assignments are not allowed")
    if first[0] in PYTHONS:
        if len(first) < 2 or first[1].startswith("-"):
            raise Bad("python only runs project scripts (no -c, -m or other flags)")
        name = Path(first[1]).name
        if first[1] not in (f"scripts/{name}", str(ROOT / "scripts" / name)):
            raise Bad(f"not an agent script: {first[1]}")
        first = [f"scripts/{name}", *first[2:]]
    elif first[0] == "cd":
        if len(first) != 2 or rel(first[1]) != "." or rest:
            raise Bad("cd only to the project root")
        first = ["cd"]
    return {"op": p["op"], "pipeline": [first, *rest]}


def validate_plan(plan) -> None:
    """Raise Bad unless the (normalized) plan is inside the grammar. Used by the hook and by agent_exec.py."""
    if not isinstance(plan, list) or not plan:
        raise Bad("empty plan")
    for k, p in enumerate(plan):
        if not isinstance(p, dict) or set(p) != {"op", "pipeline"}:
            raise Bad("malformed plan")
        if (p["op"] is None) != (k == 0) or (k and p["op"] not in OPS):
            raise Bad("malformed plan operators")
        stages = p["pipeline"]
        if not isinstance(stages, list) or not stages or not all(
                isinstance(s, list) and s and all(isinstance(w, str) for w in s) for s in stages):
            raise Bad("malformed pipeline")
        first, *filters = stages
        if first == ["cd"]:
            if filters or k == len(plan) - 1 or plan[k + 1]["op"] != "&&":
                raise Bad("cd only as `cd <project root> && …`")
        elif first[:2] == ["git", "status"]:
            if not set(first[2:]) <= {"-s", "--short", "--porcelain"}:
                raise Bad("git status only with -s/--short/--porcelain")
        elif re.fullmatch(r"scripts/[a-z_]+\.py", first[0]):
            name = first[0].split("/", 1)[1]
            if name not in SCRIPTS:
                raise Bad(f"not an agent script: {first[0]} (allowed: {', '.join(sorted(SCRIPTS))})")
            SCRIPTS[name](first[1:])
        else:
            raise Bad(f"command not allowed: {first[0]}")
        for f in filters:
            check_filter(f)


def encode_plan(plan: list[dict]) -> str:
    return base64.urlsafe_b64encode(json.dumps(plan, separators=(",", ":")).encode()).decode()


def plan_command(cmd) -> list[dict]:
    if isinstance(cmd, dict):  # {"command": ..., "args": [...]}
        cmd = shlex.join([str(cmd.get("command", ""))] + [str(x) for x in cmd.get("args") or []])
    plan = parse(str(cmd))
    validate_plan(plan)
    return plan


# ---------- the hook ------------------------------------------------------------------------------------------
def patch_paths(patch: str) -> list[str]:
    return [m.strip() for m in re.findall(r"^\*\*\* (?:Add File|Update File|Delete File|Move to): (.+)$", patch, re.M)]


EDIT_REFUSAL = ("files are not edited directly: add searches and websites with "
                "scripts/add_queries.py; everything else is the user's to change")


def check(tool: str, inp: dict) -> str | None:
    """None if the call may run, else the reason it needs approval."""
    inp = inp if isinstance(inp, dict) else {}
    if tool == "run_commands":
        cmds = inp.get("commands") or []
        if not isinstance(cmds, list) or not cmds:
            return "no commands given"
        try:
            for c in cmds:
                plan_command(c)
        except Bad as e:
            return str(e)
        return None
    if tool == "read_files":
        files = inp.get("files") or inp.get("paths") or []
        for f in files if isinstance(files, list) else [files]:
            path = f.get("path", "") if isinstance(f, dict) else str(f)
            r = rel(path)
            if r is None or r.startswith(NO_READ) or (Path(r).name.startswith(".env") and r != ".env.example"):
                return f"reading {path} is not allowed (use the scripts' JSON output or inspect_artifact.py)"
        return None
    if tool in ("editor", "apply_patch"):
        return EDIT_REFUSAL
    if tool in ("get_skill", "list_skills", "skills", "save_handoff", "list_agent_presets", "submit_and_exit",
                "get_background_command"):
        return None
    if tool == "ask_question":
        return "nobody can answer: decide yourself and log the decision"
    return f"tool {tool} needs the user's approval"


def allowed_input(tool: str, inp: dict) -> dict | None:
    """Rewritten input of an allowed call: commands run through the shell-free runner, never through bash."""
    if tool != "run_commands":
        return None
    return {**inp, "commands": [f"{RUNNER} {encode_plan(plan_command(c))}" for c in inp["commands"]]}


def blocked_input(tool: str, inp: dict, reason: str) -> dict:
    """A harmless replacement call that tells the agent why the original was not run."""
    msg = f"BLOCKED by tool guard (needs the user's approval): {reason}. Allowed: .venv/bin/python scripts/<agent script>.py, git status."
    if "query_plan" in json.dumps(inp) or tool in ("editor", "apply_patch"):
        msg += (' To add queries: .venv/bin/python scripts/add_queries.py --year-tag 2008 --query "..." '
                '(repeat --query, max 8; --add-domain example.com for a new website).')
    if tool == "run_commands":
        return {"commands": [f"printf '%s\\n' {shlex.quote(msg)}"]}
    if tool == "read_files":
        return {"files": [{"path": str(NOTICE)}]}
    if tool in ("editor", "apply_patch"):
        return {"path": "/dev/null/blocked-by-tool-guard", "old_text": "x", "new_text": "x"} if tool == "editor" \
            else {"input": "*** Begin Patch\n*** Update File: /dev/null/blocked-by-tool-guard\n*** End Patch"}
    return {"blocked_by_tool_guard": msg}


def record(tool: str, inp: dict, reason: str) -> None:
    try:
        import report_issue
        from common import log
        log(f"tool guard blocked {tool}: {reason}", level="WARN", kind="tool_denied", tool=tool,
            reason=reason, input=json.dumps(inp)[:500])
        report_issue.report("other", f"tool guard blocked {tool}: {reason}"[:200], [json.dumps(inp)[:300]])
    except Exception:  # logging must never break the hook
        pass


def audit(tool: str, inp, reason: str | None) -> None:
    try:
        from datetime import datetime, timezone
        with AUDIT.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"), "tool": tool,
                                "allowed": reason is None, "reason": reason,
                                "input": json.dumps(inp)[:300]}) + "\n")
    except Exception:
        pass


def main() -> int:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
        call = payload.get("tool_call") or {}
        tool = call.get("name") or (payload.get("preToolUse") or {}).get("toolName") or ""
        inp = call.get("input")
        if inp is None:
            inp = (payload.get("preToolUse") or {}).get("parameters") or {}
        reason = check(tool, inp)
        rewritten = allowed_input(tool, inp) if reason is None else None
    except Exception as e:  # fail closed
        tool, inp, reason, rewritten = "unknown", {}, f"tool guard error: {type(e).__name__}: {e}", None
    audit(tool, inp, reason)
    if reason is None:
        print(json.dumps({"overrideInput": rewritten} if rewritten else {}))
        return 0
    record(tool, inp, reason)
    print(json.dumps({"overrideInput": blocked_input(tool, inp, reason),
                      "context": f"Your {tool} call was NOT executed: {reason}. Don't retry it; carry on with "
                                 "the allowed scripts, or report the problem with scripts/report_issue.py."}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
