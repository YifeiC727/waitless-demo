#!/usr/bin/env bash
# Values travel only through the child environment. Do not enable tracing here.
set +x
set -euo pipefail
if [ "$(uname -s)" != Darwin ]; then echo 'macOS Keychain is required' >&2; exit 2; fi
if [ "$#" -eq 0 ]; then echo 'usage: scripts/load-keys-macos.sh COMMAND [ARG ...]' >&2; exit 2; fi
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [ -f "$ROOT/.env" ]; then
  item_assignments="$(python3 - "$ROOT/.env" <<'PY'
import os, shlex, sys
from pathlib import Path
items = {'WAITLESS_KEYCHAIN_AWS_ID_ITEM', 'WAITLESS_KEYCHAIN_AWS_SECRET_ITEM', 'WAITLESS_KEYCHAIN_JEV_ITEM'}
for line in Path(sys.argv[1]).read_text().splitlines():
    line = line.strip()
    if line.startswith('export '): line = line[7:]
    key, sep, value = line.partition('=')
    if not sep or key not in items or key in os.environ: continue
    parsed = shlex.split(value, comments=True)
    if len(parsed) > 1: raise SystemExit('Quote Keychain item names containing spaces')
    print('export ' + key + '=' + shlex.quote(parsed[0] if parsed else ''))
PY
)"
  eval "$item_assignments"
fi
: "${WAITLESS_KEYCHAIN_AWS_ID_ITEM:?Set the Keychain service name}"
: "${WAITLESS_KEYCHAIN_AWS_SECRET_ITEM:?Set the Keychain service name}"
: "${WAITLESS_KEYCHAIN_JEV_ITEM:?Set the Keychain service name}"
AWS_ACCESS_KEY_ID="$(security find-generic-password -s "$WAITLESS_KEYCHAIN_AWS_ID_ITEM" -w 2>/dev/null)"
AWS_SECRET_ACCESS_KEY="$(security find-generic-password -s "$WAITLESS_KEYCHAIN_AWS_SECRET_ITEM" -w 2>/dev/null)"
WAITLESS_JEV_API_KEY="$(security find-generic-password -s "$WAITLESS_KEYCHAIN_JEV_ITEM" -w 2>/dev/null)"
if [ -z "$AWS_ACCESS_KEY_ID" ] || [ -z "$AWS_SECRET_ACCESS_KEY" ] || [ -z "$WAITLESS_JEV_API_KEY" ]; then
  echo 'A required Keychain credential is empty' >&2; exit 2
fi
export AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY WAITLESS_JEV_API_KEY
exec "$@"
