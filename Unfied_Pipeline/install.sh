#!/usr/bin/env bash
#
# First-time setup for nse-unified-pipeline:
#
#   ./install.sh                                      # guided (arrow keys; Enter = recommended)
#   ./install.sh --defaults                           # accept every recommended option
#   ./install.sh --dry-run                            # show what would happen, change nothing
#
# Checks prerequisites, then launches scripts/setup.py (Python standard library only), which sets up
# .venv, SearXNG (docker compose), optional search API keys, OCR, the Cline CLI and the agent model.
# Nothing is changed before you confirm its summary.
set -euo pipefail

info() { printf '\033[38;5;39m◆\033[0m %s\n' "$1"; }
warn() { printf '\033[38;5;214m!\033[0m %s\n' "$1"; }
die()  { printf '\033[38;5;196m✗\033[0m %s\n' "$1" >&2; exit 1; }

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
cd "$ROOT"
[ -f scripts/setup.py ] || die "run this from the nse-unified-pipeline root (scripts/setup.py not found)"

# --- prerequisites ---------------------------------------------------------
command -v python3 >/dev/null 2>&1 || die "python3 (3.9+) is required: https://www.python.org/downloads/"
python3 -c 'import sys; sys.exit(sys.version_info < (3, 9))' || die "python3 is $(python3 -V 2>&1); 3.9+ is required."

command -v git >/dev/null 2>&1 || warn "git not found (needed for commits and the key-safety check)."
command -v docker >/dev/null 2>&1 || warn "docker not found: needed to run SearXNG (Docker Desktop: https://www.docker.com/products/docker-desktop/)."
command -v npm >/dev/null 2>&1 || warn "npm not found: needed for the Cline agent (Node.js 20+: https://nodejs.org/)."
if [ "$(uname)" = "Darwin" ] && ! command -v brew >/dev/null 2>&1; then
  warn "Homebrew not found: needed to install tesseract (OCR) on macOS (https://brew.sh)."
fi
curl -sf -m 3 http://localhost:1234/v1/models >/dev/null 2>&1 \
  || warn "LM Studio isn't answering at localhost:1234 (only needed for the agent: https://lmstudio.ai)."

# --- launch ----------------------------------------------------------------
info "Launching setup…"
echo
exec python3 scripts/setup.py "$@"
