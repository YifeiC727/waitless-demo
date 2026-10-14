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
if [ "$WAITLESS_MODE" = live-sim ]; then
  exec python3 - "$ROOT/server" <<'PY'
import os, signal, socket, subprocess, sys, time
from urllib.parse import urlunsplit

company = app = None
def stop(signum, frame):
    raise SystemExit(128 + signum)
signal.signal(signal.SIGINT, stop)
signal.signal(signal.SIGTERM, stop)
try:
    required = ('WAITLESS_COMPANY_HOST', 'WAITLESS_COMPANY_PORT', 'WAITLESS_HOST', 'WAITLESS_PORT')
    missing = [name for name in required if not os.environ.get(name, '').strip()]
    if missing:
        print('missing required configuration: ' + ', '.join(missing), file=sys.stderr)
        raise SystemExit(2)
    try:
        port = int(os.environ['WAITLESS_COMPANY_PORT'])
        if not 1 <= port <= 65535:
            raise ValueError
    except ValueError:
        print('invalid WAITLESS_COMPANY_PORT', file=sys.stderr)
        raise SystemExit(2)
    command = ['uv', 'run', '--project', sys.argv[1], 'waitless']
    company = subprocess.Popen(command + ['company'])
    deadline = time.monotonic() + 20
    while True:
        code = company.poll()
        if code is not None:
            print('Company process exited during startup', file=sys.stderr)
            raise SystemExit(code or 1)
        try:
            with socket.create_connection((os.environ['WAITLESS_COMPANY_HOST'], port), timeout=0.2):
                break
        except OSError:
            if time.monotonic() >= deadline:
                print('Company process did not become ready', file=sys.stderr)
                raise SystemExit(1)
            time.sleep(0.05)
    app = subprocess.Popen(command + ['serve'])
    host = os.environ['WAITLESS_HOST']
    if ':' in host and not host.startswith('['):
        host = '[' + host + ']'
    print(urlunsplit(('http', host + ':' + os.environ['WAITLESS_PORT'], '', '', '')), flush=True)
    while app.poll() is None:
        code = company.poll()
        if code is not None:
            print('Company process exited while serving', file=sys.stderr)
            raise SystemExit(code or 1)
        time.sleep(0.05)
    raise SystemExit(app.returncode)
finally:
    for child in (app, company):
        if child is None:
            continue
        if child.poll() is None:
            child.terminate()
        try:
            child.wait(timeout=5)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait()
PY
fi
exec uv run --project "$ROOT/server" waitless serve
