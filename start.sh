#!/bin/bash
# Do not allow inherited shell tracing to reveal credentials.
set +x
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"

if [ ! -f .env ]; then
    printf 'Missing .env. Copy .env.example to .env and fill in the Kasa login.\n' >&2
    exit 1
fi
if [ ! -x bin/go2rtc ]; then
    /bin/bash scripts/setup.sh
fi
exec /usr/bin/python3 -m app "$@"
