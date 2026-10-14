#!/usr/bin/env bash
# Values travel only through the child environment. Do not enable tracing here.
set +x
set -euo pipefail
if [ "$(uname -s)" != Darwin ]; then echo 'macOS Keychain is required' >&2; exit 2; fi
if [ "$#" -eq 0 ]; then echo 'usage: scripts/load-keys-macos.sh COMMAND [ARG ...]' >&2; exit 2; fi
: "${WAITLESS_KEYCHAIN_AWS_ACCESS_KEY_ID:?Set the Keychain service name}"
: "${WAITLESS_KEYCHAIN_AWS_SECRET_ACCESS_KEY:?Set the Keychain service name}"
: "${WAITLESS_KEYCHAIN_JEV_API_KEY:?Set the Keychain service name}"
AWS_ACCESS_KEY_ID="$(security find-generic-password -s "$WAITLESS_KEYCHAIN_AWS_ACCESS_KEY_ID" -w 2>/dev/null)"
AWS_SECRET_ACCESS_KEY="$(security find-generic-password -s "$WAITLESS_KEYCHAIN_AWS_SECRET_ACCESS_KEY" -w 2>/dev/null)"
WAITLESS_JEV_API_KEY="$(security find-generic-password -s "$WAITLESS_KEYCHAIN_JEV_API_KEY" -w 2>/dev/null)"
if [ -z "$AWS_ACCESS_KEY_ID" ] || [ -z "$AWS_SECRET_ACCESS_KEY" ] || [ -z "$WAITLESS_JEV_API_KEY" ]; then
  echo 'A required Keychain credential is empty' >&2; exit 2
fi
export AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY WAITLESS_JEV_API_KEY
exec "$@"
