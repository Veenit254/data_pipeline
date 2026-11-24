"""Shell-free runner for the collecting agent's commands (written by scripts/tool_guard.py, never by the agent).

The tool guard rewrites every allowed run_commands call into `.venv/bin/python scripts/agent_exec.py <token>`.
The token is the parsed plan (base64 JSON). This runner validates the plan again with tool_guard.validate_plan,
so a token written by hand gains nothing. It then runs each script with an argv list (no shell: nothing is expanded,
redirected or globbed). && / || / ; behave as in a shell. stderr is merged into stdout, and pipe filters
(head/tail/wc/grep/sort/uniq/cut) are implemented here and only ever see the previous stage's output.
"""
from __future__ import annotations

import base64
import binascii
import json
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import tool_guard  # noqa: E402


class Refused(Exception):
    pass


def decode(token: str) -> list[dict]:
    try:
        plan = json.loads(base64.urlsafe_b64decode(token.encode()))
        tool_guard.validate_plan(plan)
    except (binascii.Error, ValueError, TypeError, AttributeError) as e:  # tool_guard.Bad is a ValueError
        raise Refused(str(e)) from None
    return plan


def python() -> str:
    venv = ROOT / ".venv" / "bin" / "python"
    return str(venv) if venv.exists() else sys.executable


def run_first(argv: list[str]) -> tuple[int, str]:
    if argv == ["cd"]:
        return 0, ""
    cmd = ["git", *argv[1:]] if argv[0] == "git" else [python(), str(ROOT / argv[0]), *argv[1:]]
    r = subprocess.run(cmd, cwd=ROOT, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                       env=os.environ.copy())
    return r.returncode, r.stdout.decode("utf-8", errors="replace")


# ---------- filters --------------------------------------------------------------------------------------------
def bre_to_python(p: str) -> str:
    """POSIX basic regex -> Python: in BRE  | + ? ( ) { }  are literal and their backslashed forms are special."""
    out, i = [], 0
    while i < len(p):
        ch = p[i]
        if ch == "\\" and i + 1 < len(p):
            nxt = p[i + 1]
            out.append(nxt if nxt in "|+?(){}" else "\\" + nxt)
            i += 2
            continue
        out.append("\\" + ch if ch in "|+?(){}" else ch)
        i += 1
    return "".join(out)


def _ranges(spec: str) -> list[tuple[int, int]]:
    out = []
    for part in spec.split(","):
        a, _, b = part.partition("-")
        lo = int(a) if a else 1
        hi = int(b) if b else (lo if "-" not in part else 10**9)
        out.append((lo, hi))
    return out


def _pick(items: list[str], spec: str) -> list[str]:
    rs = _ranges(spec)
    return [x for i, x in enumerate(items, 1) if any(lo <= i <= hi for lo, hi in rs)]


def filter_lines(words: list[str], lines: list[str]) -> tuple[list[str], int]:
    """Apply one (already validated) filter. Returns (lines, exit status)."""
    seen, pos = tool_guard.check_filter(words)
    name = words[0]
    num = int(seen["n"][-1]) if "n" in seen else 10
    if name == "head":
        return lines[:num], 0
    if name == "tail":
        return (lines[-num:] if num else []), 0
    if name == "wc":
        text = "\n".join(lines) + ("\n" if lines else "")
        counts = {"l": len(lines), "w": len(text.split()), "c": len(text.encode()), "m": len(text)}
        keys = [k for k in "lwcm" if k in seen] or ["l", "w", "c"]
        return [" ".join(str(counts[k]) for k in keys)], 0
    if name == "grep":
        pat = (seen.get("e") or pos)[0]
        flags = re.I if "i" in seen else 0
        if "F" in seen:
            rx = re.escape(pat)
        else:
            rx = pat if "E" in seen else bre_to_python(pat)
        if "w" in seen:
            rx = rf"\b(?:{rx})\b"
        if "x" in seen:
            rx = rf"(?:{rx})\Z"
        cre = re.compile(rx, flags)
        hit = [i for i, ln in enumerate(lines) if (cre.match(ln) if "x" in seen else cre.search(ln))]
        if "v" in seen:
            hit = [i for i in range(len(lines)) if i not in set(hit)]
        if "m" in seen:
            hit = hit[:int(seen["m"][-1])]
        if "c" in seen:
            return [str(len(hit))], 0 if hit else 1
        ctx_a = int((seen.get("A") or seen.get("C") or ["0"])[-1])
        ctx_b = int((seen.get("B") or seen.get("C") or ["0"])[-1])
        keep = sorted({j for i in hit for j in range(max(0, i - ctx_b), min(len(lines), i + ctx_a + 1))})
        out = []
        for i in keep:
            ln = lines[i]
            if "o" in seen and i in hit and "v" not in seen:
                out += [m.group(0) for m in cre.finditer(ln)]
                continue
            out.append(f"{i + 1}:{ln}" if "n" in seen else ln)
        return out, 0 if hit else 1
    if name == "sort":
        sep = seen["t"][-1] if "t" in seen else None

        def field(ln: str) -> str:
            if "k" not in seen:
                return ln
            a, _, b = seen["k"][-1].partition(",")
            parts = ln.split(sep) if sep else ln.split()
            return (sep or " ").join(parts[int(a) - 1:int(b) if b else None])

        def key(ln: str):
            f = field(ln)
            if "n" in seen:
                m = re.match(r"\s*(-?\d+(?:\.\d+)?)", f)
                return (float(m.group(1)) if m else 0.0, f)
            return (f.lower() if "f" in seen else f,)
        out = sorted(lines, key=key, reverse="r" in seen)
        if "u" in seen:
            out = list(dict.fromkeys(out))
        return out, 0
    if name == "uniq":
        groups: list[list] = []
        for ln in lines:
            k = ln.lower() if "i" in seen else ln
            if groups and groups[-1][0] == k:
                groups[-1][2] += 1
            else:
                groups.append([k, ln, 1])
        if "d" in seen:
            groups = [g for g in groups if g[2] > 1]
        if "u" in seen:
            groups = [g for g in groups if g[2] == 1]
        return [f"{g[2]:>7} {g[1]}" if "c" in seen else g[1] for g in groups], 0
    if name == "cut":
        if "f" in seen:
            d = seen["d"][-1] if "d" in seen else "\t"
            return [d.join(_pick(ln.split(d), seen["f"][-1])) if d in ln else ln for ln in lines], 0
        spec = (seen.get("c") or seen.get("b"))[-1]
        return ["".join(_pick(list(ln), spec)) for ln in lines], 0
    raise Refused(f"unknown filter {name}")


def run_pipeline(stages: list[list[str]]) -> int:
    rc, out = run_first(stages[0])
    if len(stages) == 1:
        sys.stdout.write(out)
        sys.stdout.flush()
        return rc
    lines = out.splitlines()
    for f in stages[1:]:
        lines, rc = filter_lines(f, lines)
    if lines:
        sys.stdout.write("\n".join(lines) + "\n")
    sys.stdout.flush()
    return rc


def main() -> int:
    if len(sys.argv) != 2:
        print("agent_exec.py <token> (written by scripts/tool_guard.py)", file=sys.stderr)
        return 2
    try:
        plan = decode(sys.argv[1])
    except Refused as e:
        print(f"BLOCKED by agent_exec: {e}")
        return 126
    rc = 0
    for p in plan:
        if (p["op"] == "&&" and rc != 0) or (p["op"] == "||" and rc == 0):
            continue
        rc = run_pipeline(p["pipeline"])
    return rc


if __name__ == "__main__":
    sys.exit(main())
