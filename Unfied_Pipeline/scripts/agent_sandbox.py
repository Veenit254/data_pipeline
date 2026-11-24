"""OS-level confinement for the unattended agent (macOS sandbox-exec profile).

The tool guard decides which commands the agent may run; this profile is the second, independent layer that
the kernel enforces for Cline and every process it starts (hooks, scripts, workers):

- your home directory is unreadable and unwritable, except: the project directory, Cline's own state
  (~/.cline, or --config/--data-dir), and the runtimes the pipeline needs (the Python install behind .venv,
  the npm cache) which are read-only;
- inside the project, `.env*` (except .env.example) and `.git/config` are unreadable;
- ~/.ssh, ~/.aws, ~/.config/nse-unified-pipeline (search API keys), ~/Library/Keychains and the keychain service
  (com.apple.SecurityServer) are denied explicitly, even if a rule above would allow them;
- writes go only to the project, Cline's state and temp directories;
- network: optional --local-network-only (localhost only: LM Studio, SearXNG). The collection pipeline itself
  downloads from the web, so run_agent.sh leaves outbound network open by default.

Consequence: paid search keys (Serper/Brave) in ~/.config/nse-unified-pipeline/keys.env are NOT readable inside the
agent's sandbox; agent sessions search through SearXNG. Pass --allow-keys to permit reading that one file
(the trade-off: a guard bypass could then read the keys too).

sandbox-exec is deprecated by Apple but still enforced by the kernel on current macOS; it changes nothing on
your account, permissions or keychain and applies only to the processes it starts.

  python scripts/agent_sandbox.py --print                 # the profile for this checkout
  python scripts/agent_sandbox.py --check                 # verify the confinement (exit 1 if a probe leaks)
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HOME = Path(os.path.expanduser("~")).resolve()
SECRET_DIRS = [".ssh", ".aws", ".gnupg", ".config/gh", ".config/nse-unified-pipeline", ".netrc", ".docker",
               "Library/Keychains", ".kube", ".npmrc", ".pypirc", ".git-credentials", ".gitconfig"]


def q(p: Path | str) -> str:
    return '"' + str(p).replace("\\", "\\\\").replace('"', '\\"') + '"'


def runtime_dirs() -> list[Path]:
    """Read-only locations under $HOME that the pipeline's runtimes need."""
    out = []
    venv_py = ROOT / ".venv" / "bin" / "python"
    try:
        base = subprocess.run([str(venv_py), "-c", "import sys; print(sys.base_prefix)"], capture_output=True,
                              text=True, timeout=30).stdout.strip()
        if base:
            out.append(Path(base).resolve())
    except (OSError, subprocess.SubprocessError):
        pass
    venv = (ROOT / ".venv").resolve()
    if venv.exists() and not venv.is_relative_to(ROOT):
        out.append(venv)                         # a .venv symlinked to another checkout
    nm = (ROOT / "node_modules").resolve()
    if nm.exists() and not nm.is_relative_to(ROOT):
        out.append(nm)
    out.append(HOME / ".npm")
    return [p for p in out if p.is_relative_to(HOME)]


def profile(cline_dirs: list[Path] | None = None, allow_keys: bool = False, local_network_only: bool = False) -> str:
    root = ROOT.resolve()
    cline_dirs = [p.resolve() for p in (cline_dirs or [HOME / ".cline"])]
    tmp = [Path(tempfile.gettempdir()).resolve(), Path("/private/tmp"), Path("/private/var/folders")]
    lines = ["(version 1)", "(allow default)",
             ";; home: nothing, except what is allowed below",
             f"(deny file-read* file-write* (subpath {q(HOME)}))",
             # metadata of the path down to the project (realpath, cwd) is needed; contents are not
             f"(allow file-read-metadata (subpath {q(HOME)}))",
             f"(allow file-read* file-write* (subpath {q(root)}))"]
    for d in cline_dirs:
        lines.append(f"(allow file-read* file-write* (subpath {q(d)}))")
    for d in runtime_dirs():
        lines.append(f"(allow file-read* (subpath {q(d)}))")
    lines += [";; writes: project, Cline state, temp only",
              "(deny file-write* (require-not (require-any "
              + " ".join(f"(subpath {q(p)})" for p in [root, *cline_dirs, *tmp]) + " (literal \"/dev/null\")"
              + " (literal \"/dev/tty\") (regex #\"^/dev/fd/\") (regex #\"^/dev/ttys\"))))",
              ";; secrets inside the project",
              f"(deny file-read* (regex #\"^{root}/\\.env\") (literal {q(root / '.git' / 'config')}))",
              f"(allow file-read* (literal {q(root / '.env.example')}))",
              ";; secrets in home and the keychain, whatever was allowed above"]
    for s in SECRET_DIRS:
        p = HOME / s
        if s == ".config/nse-unified-pipeline" and allow_keys:
            continue
        lines.append(f"(deny file-read* file-write* (subpath {q(p)}))")
    lines.append('(deny mach-lookup (global-name "com.apple.SecurityServer") (global-name "com.apple.securityd"))')
    if local_network_only:
        lines += ["(deny network-outbound)", '(allow network-outbound (remote ip "localhost:*"))',
                  "(allow network-outbound (remote unix-socket))"]
    return "\n".join(lines) + "\n"


PROBES = [  # (description, argv, must_succeed)
    ("read a project file", ["/bin/cat", str(ROOT / "scripts" / "agent_sandbox.py")], True),
    ("write in the project", ["/usr/bin/touch", str(ROOT / "logs" / ".sandbox_probe")], True),
    ("run the venv python", [str(ROOT / ".venv" / "bin" / "python"), "-c", "import pandas, requests"], True),
    ("list home", ["/bin/ls", str(HOME)], False),
    ("read ~/.ssh", ["/bin/ls", str(HOME / ".ssh")], False),
    ("read ~/.zshrc", ["/bin/cat", str(HOME / ".zshrc")], False),
    ("read the search keys file", ["/bin/cat", str(HOME / ".config" / "nse-unified-pipeline" / "keys.env")], False),
    ("read the project .env", ["/bin/cat", str(ROOT / ".env")], False),
    ("write in home", ["/usr/bin/touch", str(HOME / ".sandbox_probe_should_fail")], False),
    ("keychain access", ["/usr/bin/security", "list-keychains"], False),
]


def check(prof: str) -> int:
    with tempfile.NamedTemporaryFile("w", suffix=".sb", delete=False) as f:
        f.write(prof)
    bad = 0
    for desc, argv, want in PROBES:
        if not want and not Path(argv[-1]).exists() and argv[0] in ("/bin/cat", "/bin/ls"):
            print(f"  skip  {desc} (no such file)")
            continue
        r = subprocess.run(["sandbox-exec", "-f", f.name, *argv], capture_output=True, text=True)
        ok = (r.returncode == 0) == want
        bad += not ok
        print(f"  {'ok  ' if ok else 'LEAK' if not want else 'FAIL'}  {desc}: exit {r.returncode}"
              + ("" if ok else f" {r.stderr.strip()[:120]}"))
    (ROOT / "logs" / ".sandbox_probe").unlink(missing_ok=True)
    os.unlink(f.name)
    return 1 if bad else 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter,
                                 allow_abbrev=False)
    ap.add_argument("--print", action="store_true", help="print the profile")
    ap.add_argument("--check", action="store_true", help="run probes inside the profile")
    ap.add_argument("--out", type=Path, help="write the profile to this file")
    ap.add_argument("--cline-dir", type=Path, action="append", help="Cline config/data dirs (default ~/.cline)")
    ap.add_argument("--allow-keys", action="store_true", help="allow reading ~/.config/nse-unified-pipeline")
    ap.add_argument("--local-network-only", action="store_true")
    a = ap.parse_args()
    prof = profile(a.cline_dir, a.allow_keys, a.local_network_only)
    if a.out:
        a.out.write_text(prof)
    if a.print:
        sys.stdout.write(prof)
    if a.check:
        return check(prof)
    return 0


if __name__ == "__main__":
    sys.exit(main())
