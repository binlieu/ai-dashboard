#!/bin/bash
set +x
set -uo pipefail

status=0
if cd -- "$(dirname -- "${BASH_SOURCE[0]}")"; then
    ./install.sh "$@" || status=$?
else
    status=1
fi
printf '\nPress Return to close. '
IFS= read -r response || true
exit "$status"
