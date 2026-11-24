#!/usr/bin/env bash
# Cline PreToolUse hook: every tool call of the collecting agent is checked by scripts/tool_guard.py.
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
exec "$ROOT/.venv/bin/python" "$ROOT/scripts/tool_guard.py"
