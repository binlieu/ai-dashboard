#!/bin/bash
set +x
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."

if [ "$#" -gt 1 ] || { [ "$#" -eq 1 ] && [ "$1" != --dry-run ]; }; then
    printf 'Usage: %s [--dry-run]\n' "$0" >&2
    exit 2
fi

port=$(/usr/bin/python3 -B -c '
from app.envfile import env_get

port = env_get(".env", "DASHBOARD_PORT") or "1984"
if not port.isascii() or not port.isdecimal() or len(port) > 5 or not 1 <= int(port) <= 65535:
    raise SystemExit("DASHBOARD_PORT must be a number from 1 to 65535.")
print(int(port))
')
url="http://127.0.0.1:$port/"

if [ -d "/Applications/Google Chrome.app" ]; then
    browser=(/usr/bin/open -na "Google Chrome" --args --kiosk "--app=$url"
        --autoplay-policy=no-user-gesture-required --disable-session-crashed-bubble
        --noerrdialogs --no-first-run)
else
    browser=(/usr/bin/open "$url")
fi

if [ "${1:-}" = --dry-run ]; then
    printf 'Dashboard: %s\nBrowser command:' "$url"
    printf ' %q' "${browser[@]}"
    printf '\n'
    exit 0
fi

deadline=$((SECONDS + 180))
while (( SECONDS < deadline )); do
    timeout=$((deadline - SECONDS))
    if (( timeout > 3 )); then timeout=3; fi
    if /usr/bin/curl --disable --noproxy '*' --silent --fail --max-time "$timeout" \
        --header "Host: 127.0.0.1:$port" --output /dev/null "${url}api/cameras"; then
        "${browser[@]}"
        exec /usr/bin/caffeinate -d -i -s
    fi
    if (( SECONDS < deadline )); then /bin/sleep 1; fi
done

printf 'The dashboard did not answer at %s within 180 seconds.\n' "$url" >&2
exit 1
