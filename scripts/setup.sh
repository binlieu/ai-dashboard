#!/bin/bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."

if [ "$(uname -s)" != Darwin ]; then
    printf 'unsupported platform: macOS is required.\n' >&2
    exit 1
fi

version=1.9.15
if [ -x bin/go2rtc ] &&
   bin/go2rtc -version 2>/dev/null | grep -Eq '(^|[[:space:]])1[.]9[.]15([[:space:]]|$)'; then
    exit 0
fi

case "$(uname -m)" in
    arm64)
        asset=go2rtc_mac_arm64.zip
        expected_sha=d0cf858a71803dce2377423bc830639137ee96c335f88e9912e7f7898374266d
        ;;
    x86_64)
        asset=go2rtc_mac_amd64.zip
        expected_sha=7ca6075421b824eebe0383d20bd42a6f6a7ec7591a60931d42290f1d375169b9
        ;;
    *)
        printf 'unsupported platform: this Mac architecture has no pinned binary.\n' >&2
        exit 1
        ;;
esac

download_dir=$(mktemp -d "${TMPDIR:-/tmp}/kasa-go2rtc.XXXXXX")
trap 'rm -rf -- "$download_dir"' EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
archive=$download_dir/$asset
url=https://github.com/AlexxIT/go2rtc/releases/download/v$version/$asset

printf 'Downloading go2rtc v%s...\n' "$version"
curl --fail --location --silent --show-error --retry 2 \
    --connect-timeout 15 --max-time 120 --output "$archive" "$url"
actual_sha=$(shasum -a 256 "$archive" | awk '{print $1}')
if [ "$actual_sha" != "$expected_sha" ]; then
    rm -f -- "$archive"
    printf 'go2rtc checksum mismatch; the download was deleted. Installation stopped.\n' >&2
    exit 1
fi

unzip -q "$archive" -d "$download_dir"
chmod +x "$download_dir/go2rtc"
mkdir -p bin
mv -- "$download_dir/go2rtc" bin/go2rtc
printf 'Installed go2rtc v%s.\n' "$version"
