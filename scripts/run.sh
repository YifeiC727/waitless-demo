#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
if [ "${WAITLESS_ENV_LOADED:-}" != 1 ] && [ -f .env ]; then
  # Existing exported values (including keychain credentials) take precedence.
  env_assignments="$(python3 - <<'PY'
import os, shlex
from pathlib import Path
for line in Path('.env').read_text().splitlines():
    line = line.strip()
    if not line or line.startswith('#'): continue
    if line.startswith('export '): line = line[7:]
    key, sep, value = line.partition('=')
    if not sep or not key.isidentifier(): raise SystemExit('Invalid .env assignment')
    if key in os.environ: continue
    parsed = shlex.split(value, comments=True)
    if len(parsed) > 1: raise SystemExit('Quote values containing spaces in .env')
    print('export ' + key + '=' + shlex.quote(parsed[0] if parsed else ''))
PY
)"
  eval "$env_assignments"
fi
if [ "$#" -gt 1 ]; then echo 'usage: scripts/run.sh [live-sim|text-sim|replay]' >&2; exit 2; fi
if [ "$#" -eq 1 ]; then export WAITLESS_MODE="$1"; fi
case "${WAITLESS_MODE:-}" in
  text-sim|replay) export WAITLESS_TEXTSIM_PACE_MS="${WAITLESS_TEXTSIM_PACE_MS:-1200}" ;;
  live-sim) ;;
  *) echo 'WAITLESS_MODE must be live-sim, text-sim or replay' >&2; exit 2 ;;
esac
exec uv run --project "$ROOT/server" waitless serve
