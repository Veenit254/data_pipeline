"""Guided first-time setup for nse-unified-pipeline. Launched by ./install.sh.

Arrow through 9 screens; the first option on every screen is the recommended one (Enter accepts it).
The panel under the options shows what the highlighted option will do and what is already in place.
Nothing is changed until you confirm the summary; files are backed up (.bak, .bak1, ...) before they are
written, existing secrets are never replaced, and API keys are typed hidden and never printed.

  ./install.sh                     # interactive (arrow keys; numbered prompts without a terminal UI)
  ./install.sh --defaults          # accept every recommended option, no questions (skips key entry)
  ./install.sh --dry-run           # show what would happen, change nothing

Standard library only: it runs before .venv exists.
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import secrets
import shutil
import subprocess
import sys
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VENV_PY = ROOT / ".venv" / "bin" / "python"
ENV = ROOT / ".env"
ENV_EXAMPLE = ROOT / ".env.example"
PLAN = ROOT / "config" / "query_plan.json"
LIMIT_DEFAULTS = {"max_files": 60, "max_companies": 25, "max_docs_per_company": 5}
KEYS_FILE = Path.home() / ".config" / "nse-unified-pipeline" / "keys.env"
SEARXNG_URL = "http://localhost:8082/search?q=test&format=json"
LMSTUDIO_URL = "http://localhost:1234/v1/models"
API_KEYS = ("SERPER_API_KEY", "BRAVE_API_KEY")
MODELS = ("qwen/qwen3.8-27b", "google/gemma-4-26b-a4b")
RUNTIME_IMPORTS = "import requests, bs4, lxml, pdfplumber, pandas, openpyxl"


# ---------------------------------------------------------------------------
# What is already in place
# ---------------------------------------------------------------------------
def _run_ok(cmd: list[str], timeout: int = 20) -> tuple[bool, str]:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, cwd=ROOT)
        return r.returncode == 0, (r.stdout or r.stderr).strip().splitlines()[0] if (r.stdout or r.stderr).strip() else ""
    except (OSError, subprocess.SubprocessError):
        return False, ""


def _url_ok(url: str) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=5) as r:  # noqa: S310 (localhost only)
            return r.status == 200
    except Exception:
        return False


def read_env(path: Path) -> dict[str, str]:
    out = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            s = line.strip()
            if s and not s.startswith("#") and "=" in s:
                k, v = s.removeprefix("export ").split("=", 1)
                out[k.strip()] = v.strip().strip("'\"")
    return out


def run_mode() -> str | None:
    try:
        return (json.loads(PLAN.read_text(encoding="utf-8")).get("run_limits") or {}).get("mode")
    except (OSError, ValueError):
        return None


def set_run_mode(mode: str) -> None:
    """Write run_limits.mode (and default caps if missing) into config/query_plan.json, after a backup."""
    cfg = json.loads(PLAN.read_text(encoding="utf-8"))
    rl = cfg.get("run_limits") or {}
    cfg["run_limits"] = {"mode": mode, **{k: rl.get(k, v) for k, v in LIMIT_DEFAULTS.items()}}
    backup(PLAN)
    PLAN.write_text(json.dumps(cfg, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def tracked_by_git(path: Path) -> bool:
    ok, _ = _run_ok(["git", "-C", str(ROOT), "ls-files", "--error-unmatch", str(path)])
    return ok


def detect() -> dict:
    env, keys = read_env(ENV), read_env(KEYS_FILE)
    venv = VENV_PY.exists()
    st = {
        "python": platform.python_version(),
        "venv": venv,
        "venv_deps": venv and _run_ok([str(VENV_PY), "-c", RUNTIME_IMPORTS])[0],
        "dev_deps": venv and _run_ok([str(VENV_PY), "-c", "import pytest, pyflakes"])[0],
        "docker": shutil.which("docker") is not None,
        "searxng": _url_ok(SEARXNG_URL),
        "env_file": ENV.exists(),
        "env_secret": bool(env.get("SEARXNG_SECRET")),
        "env_tracked": ENV.exists() and tracked_by_git(ENV),
        "keys": {k: ("environment" if os.environ.get(k) else "~/.config/nse-unified-pipeline/keys.env" if keys.get(k)
                     else ".env" if env.get(k) else None) for k in API_KEYS},
        "brew": shutil.which("brew") is not None,
        "apt": shutil.which("apt-get") is not None,
        "tesseract": _run_ok(["tesseract", "--version"])[1] if shutil.which("tesseract") else None,
        "npm": shutil.which("npm") is not None,
        "cline": (ROOT / "node_modules" / ".bin" / "cline").exists(),
        "lmstudio": _url_ok(LMSTUDIO_URL),
        "model": env.get("CLINE_MODEL") or None,
        "run_mode": run_mode(),
    }
    return st


# ---------------------------------------------------------------------------
# Screens (option 1 = recommended) and the actions each choice implies
# ---------------------------------------------------------------------------
@dataclass
class Action:
    what: str                          # one line for the summary / preview
    cmd: list[str] | None = None       # command to run, or
    fn: object = None                  # a callable doing the work (file writes)
    note: bool = False                 # informational only (nothing to do)


@dataclass
class Screen:
    key: str
    title: str
    help: str
    options: list[tuple[str, str]] = field(default_factory=list)


def screens(st: dict) -> list[Screen]:
    cur_model = st.get("model")
    return [
        Screen("python", "Python environment", "The pipeline's packages live in .venv.",
               [("create", "Create .venv and install requirements.txt (recommended)"),
                ("reuse", "Use the existing .venv as it is"), ("skip", "Skip")]),
        Screen("dev", "Developer tools", "pytest + pyflakes, used by scripts/check.sh.",
               [("install", "Install requirements-dev.txt (recommended)"), ("skip", "Skip")]),
        Screen("searxng", "SearXNG search", "The free, local search provider (docker-compose.yml).",
               [("compose", "Start it with docker compose, secret generated into .env (recommended)"),
                ("existing", "I already run SearXNG at localhost:8082"),
                ("skip", "Skip (use only Serper / Brave API keys)")]),
        Screen("keys", "Search API keys (Serper.dev, Brave)", "Optional fallbacks when SearXNG's engines are banned. "
               "Typed hidden, never printed.",
               [("keyfile", "Save to ~/.config/nse-unified-pipeline/keys.env, outside the repo (recommended)"),
                ("dotenv", "Save to the repo's .env (git-ignored)"), ("skip", "Skip for now")]),
        Screen("ocr", "OCR for scanned PDFs", "tesseract reads image-only delisting notices.",
               [("install", "Install tesseract (recommended)"), ("skip", "Skip")]),
        Screen("agent", "Agent (Cline CLI)", "Runs the collection unattended with a local LM Studio model.",
               [("install", "npm install (Cline pinned to 3.0.75) (recommended)"),
                ("skip", "Skip (run scripts/collect.py without the agent)")]),
        Screen("model", "Agent model (LM Studio)", "Saved as CLINE_MODEL in .env; scripts/run_agent.sh uses it.",
               [(MODELS[0], f"{MODELS[0]} (recommended)"), (MODELS[1], MODELS[1]),
                ("keep", f"Keep current ({cur_model})" if cur_model else "Don't set (run_agent.sh default)")]),
        Screen("limits", "Run limits", "Caps per collection run (run_limits in config/query_plan.json; "
               "collect.py flags override them).",
               [("capped", "Capped: 60 files, 25 companies, 5 documents per company per run (recommended)"),
                ("unlimited", "Unlimited: keep going until the queries, the docket or the sources are used up"),
                ("ask", "Ask at the start of each run in a terminal (unattended runs use the caps)")]),
        Screen("finish", "Finish", "Verify everything in one go.",
               [("check", "Run scripts/check.sh (recommended)"), ("skip", "Skip")]),
    ]


def defaults(st: dict) -> dict[str, str]:
    return {s.key: s.options[0][0] for s in screens(st)}


def backup(path: Path) -> Path | None:
    if not path.exists():
        return None
    b, n = path.with_name(path.name + ".bak"), 1
    while b.exists():
        b, n = path.with_name(f"{path.name}.bak{n}"), n + 1
    shutil.copy2(path, b)
    return b


def merge_env(path: Path, updates: dict[str, str], overwrite: set[str] = frozenset(),
              template: Path | None = None) -> list[str]:
    """Set keys in a KEY=value file: only empty or missing ones, unless listed in `overwrite`. Creates the
    file (from `template` if given) with mode 600, backs it up before changing it. Returns the keys set."""
    if path.exists():
        lines = path.read_text(encoding="utf-8").splitlines()
    elif template and template.exists():
        lines = template.read_text(encoding="utf-8").splitlines()
    else:
        lines = []
    changed, seen = [], set()
    for i, line in enumerate(lines):
        s = line.strip()
        if s.startswith("#") or "=" not in s:
            continue
        k, v = s.removeprefix("export ").split("=", 1)
        k = k.strip()
        if k in updates:
            seen.add(k)
            if not v.strip() or k in overwrite:
                if v.strip() != updates[k]:
                    lines[i] = f"{k}={updates[k]}"
                    changed.append(k)
    for k, v in updates.items():
        if k not in seen:
            lines.append(f"{k}={v}")
            changed.append(k)
    if changed or not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        backup(path)
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        path.chmod(0o600)
    return changed


def plan(choice: dict[str, str], st: dict, keys: dict[str, str] | None = None) -> dict[str, list[Action]]:
    """Actions per screen for these choices, given what is already in place."""
    keys = {k: v for k, v in (keys or {}).items() if v}
    py = str(VENV_PY)
    out: dict[str, list[Action]] = {}

    c = choice["python"]
    a: list[Action] = []
    if c == "create":
        if not st["venv"]:
            a.append(Action("create .venv", [sys.executable, "-m", "venv", str(ROOT / ".venv")]))
        if not st["venv_deps"]:
            a.append(Action("pip install -r requirements.txt", [py, "-m", "pip", "install", "-q", "-r", "requirements.txt"]))
        a = a or [Action(".venv is ready (packages import fine)", note=True)]
    elif c == "reuse":
        a = [Action(".venv ready" if st["venv_deps"] else ".venv missing or incomplete: the pipeline won't run", note=True)]
    out["python"] = a

    if choice["dev"] == "install":
        out["dev"] = [Action("pytest + pyflakes already installed", note=True)] if st["dev_deps"] else \
            [Action("pip install -r requirements-dev.txt", [py, "-m", "pip", "install", "-q", "-r", "requirements-dev.txt"])]

    c = choice["searxng"]
    if c == "compose":
        if st["searxng"]:
            out["searxng"] = [Action("SearXNG already answers JSON at localhost:8082 (compose would clash on the port)",
                                     note=True)]
        elif not st["docker"]:
            out["searxng"] = [Action("docker not found: install Docker Desktop, then re-run ./install.sh", note=True)]
        else:
            a = []
            if not st["env_secret"]:
                a.append(Action("write a random SEARXNG_SECRET into .env",
                                fn=lambda: merge_env(ENV, {"SEARXNG_SECRET": secrets.token_hex(32)}, template=ENV_EXAMPLE)))
            a.append(Action("docker compose up -d", ["docker", "compose", "up", "-d"]))
            out["searxng"] = a
    elif c == "existing":
        out["searxng"] = [Action("SearXNG answers at localhost:8082" if st["searxng"]
                                 else "nothing answers at localhost:8082 yet: start your SearXNG with JSON enabled", note=True)]

    c = choice["keys"]
    if c in ("keyfile", "dotenv"):
        found = {k: v for k, v in st["keys"].items() if v}
        a = [Action(f"{k}: already set ({where})", note=True) for k, where in found.items()]
        if keys:
            target = KEYS_FILE if c == "keyfile" else ENV
            if c == "dotenv" and st["env_tracked"]:
                a.append(Action(".env is tracked by git: refusing to put keys in it (choose the key file)", note=True))
            else:
                names = ", ".join(sorted(keys))
                a.append(Action(f"save {names} to {str(target).replace(str(Path.home()), '~')} (mode 600)",
                                fn=lambda t=target: merge_env(t, keys, overwrite=set(keys),
                                                              template=ENV_EXAMPLE if t == ENV else None)))
        elif not found:
            a.append(Action("no keys entered: SearXNG only", note=True))
        out["keys"] = a

    if choice["ocr"] == "install":
        if st["tesseract"]:
            out["ocr"] = [Action(f"{st['tesseract']} already installed", note=True)]
        elif st["brew"]:
            out["ocr"] = [Action("brew install tesseract", ["brew", "install", "tesseract"])]
        else:
            hint = "sudo apt-get install -y tesseract-ocr" if st["apt"] else "install tesseract with your package manager"
            out["ocr"] = [Action(f"run yourself: {hint}", note=True)]

    if choice["agent"] == "install":
        if st["cline"]:
            out["agent"] = [Action("Cline CLI already installed (node_modules)", note=True)]
        elif st["npm"]:
            out["agent"] = [Action("npm install", ["npm", "install", "--no-audit", "--no-fund"])]
        else:
            out["agent"] = [Action("npm not found: install Node.js 20+, then re-run ./install.sh", note=True)]
        if not st["lmstudio"]:
            out["agent"].append(Action("LM Studio isn't answering at localhost:1234: start it and load the model", note=True))

    m = choice["model"]
    if m != "keep":
        out["model"] = [Action(f"CLINE_MODEL={m} already in .env", note=True)] if st["model"] == m else \
            [Action(f"set CLINE_MODEL={m} in .env",
                    fn=lambda: merge_env(ENV, {"CLINE_MODEL": m}, overwrite={"CLINE_MODEL"}, template=ENV_EXAMPLE))]

    lm = choice.get("limits")
    if lm:
        out["limits"] = [Action(f"run limits already '{lm}'", note=True)] if st.get("run_mode") == lm else \
            [Action(f"set run_limits.mode = {lm} in config/query_plan.json (backup first)",
                    fn=lambda: set_run_mode(lm))]

    if choice["finish"] == "check":
        out["finish"] = [Action("scripts/check.sh", ["bash", "scripts/check.sh"])]
    return out


def preview(screen: Screen, value: str, choice: dict, st: dict) -> list[str]:
    trial = dict(choice, **{screen.key: value})
    acts = plan(trial, st).get(screen.key, [])
    lines = [("· " if a.note else "→ ") + a.what for a in acts] or ["· nothing"]
    if screen.key == "keys" and value != "skip":
        lines.append("· you'll be asked for each key that isn't set yet (hidden; Enter to skip)")
    return lines


# ---------------------------------------------------------------------------
# Curses TUI
# ---------------------------------------------------------------------------
def run_tui(choice: dict, st: dict) -> bool:
    import curses

    scr = screens(st)

    def draw(w, idx: int, cursor: dict):
        curses.curs_set(0)
        w.erase()
        h, wd = w.getmaxyx()

        def put(y, x, text, attr=0):
            if 0 <= y < h - 1:
                try:
                    w.addstr(y, x, text[: max(0, wd - x - 1)], attr)
                except curses.error:
                    pass
        put(0, 0, " nse-unified-pipeline setup ".ljust(wd - 1), curses.A_REVERSE)
        s = scr[idx]
        put(2, 2, f"[{idx + 1}/{len(scr)}] {s.title}", curses.A_BOLD)
        put(3, 2, s.help, curses.A_DIM)
        row = 5
        for i, (_, label) in enumerate(s.options):
            sel = i == cursor[s.key]
            put(row + i, 2, f"  {'›' if sel else ' '} {label}", curses.A_REVERSE if sel else curses.A_NORMAL)
        prow = row + len(s.options) + 1
        put(prow, 2, "this will", curses.A_DIM)
        for j, line in enumerate(preview(s, s.options[cursor[s.key]][0], choice, st)):
            put(prow + 1 + j, 4, line)
        put(h - 1, 0, "↑/↓ move · ⏎/→ next · ←/b back · q quit (nothing is changed before the summary)", curses.A_DIM)
        w.refresh()

    def loop(w):
        curses.use_default_colors()
        cursor = {s.key: next((i for i, (v, _) in enumerate(s.options) if v == choice[s.key]), 0) for s in scr}
        idx = 0
        while True:
            draw(w, idx, cursor)
            s, k = scr[idx], w.getch()
            n = len(s.options)
            if k in (ord("q"), 27):
                return False
            if k in (curses.KEY_UP, ord("k")):
                cursor[s.key] = (cursor[s.key] - 1) % n
            elif k in (curses.KEY_DOWN, ord("j")):
                cursor[s.key] = (cursor[s.key] + 1) % n
            elif k in (curses.KEY_LEFT, ord("b")) and idx > 0:
                idx -= 1
            elif k in (curses.KEY_RIGHT, curses.KEY_ENTER, 10, 13):
                choice[s.key] = s.options[cursor[s.key]][0]
                if idx == len(scr) - 1:
                    return True
                idx += 1
    return curses.wrapper(loop)


def _err(msg: str = "") -> None:
    sys.stderr.write(msg + "\n")


def run_prompts(choice: dict, st: dict, ask=input) -> bool:
    """Numbered fallback: blank answer = option 1 (recommended)."""
    for s in screens(st):
        _err(f"\n{s.title} — {s.help}")
        for i, (v, label) in enumerate(s.options, 1):
            _err(f"  {i}) {label}")
        raw = ask("  > [1] ").strip().lower()
        if raw == "q":
            return False
        if raw.isdigit() and 1 <= int(raw) <= len(s.options):
            choice[s.key] = s.options[int(raw) - 1][0]
        for line in preview(s, choice[s.key], choice, st):
            _err("    " + line)
    return True


# ---------------------------------------------------------------------------
# Apply
# ---------------------------------------------------------------------------
def ask_keys(choice: dict, st: dict, getpass_fn=None) -> dict[str, str]:
    if choice["keys"] == "skip":
        return {}
    import getpass
    getpass_fn = getpass_fn or getpass.getpass
    out = {}
    for k in API_KEYS:
        if not st["keys"][k]:
            v = getpass_fn(f"{k} (hidden, Enter to skip): ").strip()
            if v:
                out[k] = v
    return out


def summary(actions: dict[str, list[Action]]) -> list[str]:
    titles = {s.key: s.title for s in screens({"model": None})}
    lines = []
    for key, acts in actions.items():
        for a in acts:
            lines.append(f"  {'·' if a.note else '→'} {titles.get(key, key)}: {a.what}")
    return lines


def apply(actions: dict[str, list[Action]]) -> int:
    for key, acts in actions.items():
        for a in acts:
            if a.note:
                continue
            _err(f"\n◆ {a.what}")
            if a.fn:
                try:
                    a.fn()
                except Exception as e:
                    _err(f"✗ failed: {e}")
                    return 1
            elif a.cmd:
                r = subprocess.run(a.cmd, cwd=ROOT)
                if r.returncode != 0:
                    _err(f"✗ failed (exit {r.returncode}). Retry with: {' '.join(a.cmd)}  — or re-run ./install.sh")
                    return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter,
                                 allow_abbrev=False)
    ap.add_argument("--defaults", action="store_true", help="accept every recommended option without asking")
    ap.add_argument("--dry-run", action="store_true", help="show the plan, change nothing")
    a = ap.parse_args(argv)
    if sys.version_info < (3, 9):
        _err("Python 3.9+ is required.")
        return 1

    _err("◆ checking what is already set up…")
    st = detect()
    choice = defaults(st)
    interactive = sys.stdin.isatty() and sys.stdout.isatty()
    if not a.defaults:
        if interactive:
            try:
                done = run_tui(choice, st)
            except Exception as e:
                _err(f"(terminal UI unavailable: {e}; using numbered prompts)")
                done = run_prompts(choice, st)
        else:
            done = run_prompts(choice, st)
        if not done:
            _err("cancelled — nothing changed")
            return 130
    keys = {} if a.defaults or a.dry_run else ask_keys(choice, st)
    actions = plan(choice, st, keys)

    _err("\nSummary:")
    for line in summary(actions):
        _err(line)
    todo = [x for acts in actions.values() for x in acts if not x.note]
    if not todo:
        _err("\nEverything chosen is already in place.")
        return 0
    if a.dry_run:
        _err(f"\n--dry-run: {len(todo)} step(s) would run; nothing changed.")
        return 0
    if not a.defaults and interactive:
        if input(f"\nApply these {len(todo)} step(s)? [Y/n] > ").strip().lower() in ("n", "no"):
            _err("nothing changed")
            return 1
    rc = apply(actions)
    if rc == 0:
        _err("\n✓ setup done. Start the agent with scripts/run_agent.sh, or one run without it with "
             ".venv/bin/python scripts/collect.py --max-queries 5")
    return rc


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (KeyboardInterrupt, EOFError):
        _err("\naborted — steps already applied stay applied; re-run ./install.sh to continue")
        sys.exit(130)
