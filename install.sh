#!/bin/bash
set +x
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"

if [ "$(/usr/bin/uname -s)" != Darwin ]; then
    printf 'The dashboard installer requires macOS.\n' >&2
    exit 1
fi

if ! /usr/bin/xcode-select -p >/dev/null 2>&1; then
    printf 'Apple Command Line Tools are required. Follow the instructions in the macOS dialog.\n'
    /usr/bin/xcode-select --install || true
    printf 'When the Command Line Tools finish installing, run the dashboard installer again.\n'
    exit 2
fi

case "${1:-}" in
    ""|-*) set -- install "$@" ;;
esac
export PYTHONDONTWRITEBYTECODE=1
exec /usr/bin/python3 -m app.install "$@"
