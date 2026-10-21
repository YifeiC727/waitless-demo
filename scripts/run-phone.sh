#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
if [ -f .env ]; then
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
: "${WAITLESS_HOST:?Set WAITLESS_HOST}"
: "${WAITLESS_PORT:?Set WAITLESS_PORT}"
: "${WAITLESS_ALLOWED_HOSTS:?Set WAITLESS_ALLOWED_HOSTS}"
export WAITLESS_ENV_LOADED=1
phone_tmp="$(mktemp -d)"
phone_pid=''
tunnel_pid=''
cleanup() {
  for child in "$phone_pid" "$tunnel_pid"; do
    if [ -n "$child" ]; then kill "$child" 2>/dev/null || true; wait "$child" 2>/dev/null || true; fi
  done
  python3 - "$phone_tmp" <<'PY'
import shutil, sys
shutil.rmtree(sys.argv[1])
PY
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
local_url="$(python3 - <<'PY'
import os
from urllib.parse import urlunsplit
host = os.environ['WAITLESS_HOST']
if ':' in host: host = '[' + host + ']'
print(urlunsplit(('http', host + ':' + os.environ['WAITLESS_PORT'], '', '', '')))
PY
)"
cloudflared tunnel --url "$local_url" --no-autoupdate >"$phone_tmp/tunnel.log" 2>&1 &
tunnel_pid=$!
WAITLESS_PUBLIC_BASE_URL="$(python3 - "$phone_tmp/tunnel.log" <<'PY'
import re, socket, sys, time
from pathlib import Path
from urllib.parse import urlsplit
end = time.monotonic() + 90
while time.monotonic() < end:
    matches = re.findall(r'https://[a-z0-9-]+\.trycloudflare\.com', Path(sys.argv[1]).read_text())
    if matches:
        url = matches[-1]
        try:
            socket.getaddrinfo(urlsplit(url).hostname, None)
            print(url)
            break
        except socket.gaierror: pass
    time.sleep(0.5)
else:
    raise SystemExit('Tunnel hostname did not become resolvable')
PY
)"
export WAITLESS_PUBLIC_BASE_URL
tunnel_host="$(python3 - <<'PY'
import os
from urllib.parse import urlsplit
print(urlsplit(os.environ['WAITLESS_PUBLIC_BASE_URL']).netloc)
PY
)"
export WAITLESS_ALLOWED_HOSTS="$WAITLESS_ALLOWED_HOSTS,$tunnel_host"
"$ROOT/scripts/run.sh" "$@" &
phone_pid=$!
python3 - "$local_url" <<'PY'
import sys, time, urllib.request
end = time.monotonic() + 90
while time.monotonic() < end:
    try:
        with urllib.request.urlopen(sys.argv[1] + '/healthz', timeout=1) as response:
            if response.status == 200: break
    except OSError: pass
    time.sleep(0.5)
else: raise SystemExit('Application did not become ready')
PY
printf '%s\n' "$WAITLESS_PUBLIC_BASE_URL"
wait "$phone_pid"
