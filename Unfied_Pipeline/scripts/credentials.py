"""API keys for the search providers, read from the environment or from a key file. Never from code.

Lookup order for a name such as SERPER_API_KEY (first hit wins):
1. the environment variable itself;
2. the file named by NSE_KEYS_FILE (any path, e.g. outside the repo);
3. ~/.config/nse-unified-pipeline/keys.env (outside the repo: recommended);
4. .env in the project root (git-ignored; refused if it is ever tracked by git).

Key files use `NAME=value` lines; `#` comments, blank lines, `export ` prefixes and quotes are fine.
scripts/run_agent.sh removes *API_KEY* variables from the agent's environment, so for unattended runs
put the keys in one of the files: the pipeline scripts read them, the agent cannot (tool guard).
"""
from __future__ import annotations

import os
import re
import subprocess
from functools import lru_cache
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
USER_KEYS = Path.home() / ".config" / "nse-unified-pipeline" / "keys.env"
REPO_ENV = ROOT / ".env"
LINE_RE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$")


def key_files() -> list[Path]:
    files = [Path(os.environ["NSE_KEYS_FILE"]).expanduser()] if os.environ.get("NSE_KEYS_FILE") else []
    return files + [USER_KEYS, REPO_ENV]


def tracked_by_git(path: Path) -> bool:
    try:
        r = subprocess.run(["git", "-C", str(ROOT), "ls-files", "--error-unmatch", str(path)],
                           capture_output=True, timeout=10)
        return r.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def parse(text: str) -> dict[str, str]:
    out = {}
    for line in text.splitlines():
        if line.lstrip().startswith("#"):
            continue
        m = LINE_RE.match(line)
        if m:
            v = m.group(2)
            if len(v) >= 2 and v[0] == v[-1] and v[0] in "'\"":
                v = v[1:-1]
            else:
                v = re.sub(r"\s+#.*$", "", v)  # trailing comment on an unquoted value
            out[m.group(1)] = v
    return out


@lru_cache(maxsize=None)
def _file_values(path: str) -> dict[str, str]:
    p = Path(path)
    if not p.is_file():
        return {}
    if p.resolve().is_relative_to(ROOT) and tracked_by_git(p):
        raise RuntimeError(f"{p} is tracked by git: keys must not be committed. Run `git rm --cached {p}` "
                           "and rotate the keys in it.")
    return parse(p.read_text(encoding="utf-8"))


def get(name: str) -> str | None:
    """The value of a secret, or None. Never log or print what this returns."""
    if os.environ.get(name):
        return os.environ[name]
    for f in key_files():
        v = _file_values(str(f)).get(name)
        if v:
            return v
    return None


def where(name: str) -> str | None:
    """Where a secret comes from (for status output), without revealing it."""
    if os.environ.get(name):
        return "environment"
    for f in key_files():
        if _file_values(str(f)).get(name):
            return str(f).replace(str(Path.home()), "~")
    return None
