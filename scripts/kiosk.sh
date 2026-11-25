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

# Firefox first: it has a real kiosk mode and stays current on older macOS releases.
firefox_profile="$HOME/Library/Application Support/kasa-dashboard/firefox-kiosk"
if [ -d "/Applications/Firefox.app" ]; then
    browser=(/usr/bin/open -na Firefox --args --kiosk --no-remote --profile "$firefox_profile" "$url")
elif [ -d "/Applications/Google Chrome.app" ]; then
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
        if [ "${browser[2]}" = Firefox ]; then
            # A window reopened in an already running Firefox is not in kiosk mode, so
            # quit the previous kiosk copy (only that profile) and start it fresh.
            if /usr/bin/pkill -f -- "--profile $firefox_profile"; then
                for _ in 1 2 3 4 5 6 7 8 9 10; do
                    /usr/bin/pgrep -f -- "--profile $firefox_profile" >/dev/null || break
                    /bin/sleep 1
                done
            fi
            # A private profile that opens straight to the dashboard: no welcome pages,
            # no crash-restore prompt, and video may start without a click.
            /bin/mkdir -p "$firefox_profile"
            /bin/chmod 700 "$firefox_profile"
            /bin/cat > "$firefox_profile/user.js" <<'PREFS'
user_pref("browser.aboutwelcome.enabled", false);
user_pref("browser.shell.checkDefaultBrowser", false);
user_pref("browser.startup.homepage_override.mstone", "ignore");
user_pref("startup.homepage_welcome_url", "");
user_pref("startup.homepage_welcome_url.additional", "");
user_pref("trailhead.firstrun.didSeeAboutWelcome", true);
user_pref("datareporting.policy.dataSubmissionPolicyBypassNotification", true);
user_pref("toolkit.telemetry.reportingpolicy.firstRun", false);
user_pref("browser.sessionstore.resume_from_crash", false);
user_pref("browser.tabs.warnOnClose", false);
user_pref("browser.translations.automaticallyPopup", false);
user_pref("media.autoplay.default", 0);
user_pref("media.autoplay.blocking_policy", 0);
PREFS
        fi
        "${browser[@]}"
        exec /usr/bin/caffeinate -d -i -s
    fi
    if (( SECONDS < deadline )); then /bin/sleep 1; fi
done

printf 'The dashboard did not answer at %s within 180 seconds.\n' "$url" >&2
exit 1
