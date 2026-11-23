#!/bin/bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."

if [ "$(uname -s)" != Darwin ]; then
    printf 'unsupported platform: macOS is required.\n' >&2
    exit 1
fi

if ! macos_version=$(sw_vers -productVersion 2>/dev/null); then
    macos_version=
fi
macos_major=${macos_version%%.*}
if ! [[ $macos_version =~ ^[1-9][0-9]*(\.[0-9]+)*$ ]] || (( macos_major < 12 )); then
    printf 'unsupported platform: macOS 12 or newer is required.\n' >&2
    exit 1
fi

if [ "$macos_major" = 12 ]; then
    version=1.9.14
    arm64_sha=919b78adc759d6b3883d1e1b2ac915ac0985bb903ff1897b4d228527bd64690c
    amd64_sha=9b0b9a27a4dc3a5b8b93376e7e8fc2787c6af624a512842622be84aec0171c7a
else
    version=1.9.15
    arm64_sha=d0cf858a71803dce2377423bc830639137ee96c335f88e9912e7f7898374266d
    amd64_sha=7ca6075421b824eebe0383d20bd42a6f6a7ec7591a60931d42290f1d375169b9
fi

version_pattern="(^|[[:space:]])${version//./[.]}([[:space:]]|$)"
if [ -x bin/go2rtc ] &&
   installed_version=$(bin/go2rtc -version 2>/dev/null) &&
   [[ $installed_version =~ $version_pattern ]]; then
    exit 0
fi

case "$(uname -m)" in
    arm64)
        asset=go2rtc_mac_arm64.zip
        expected_sha=$arm64_sha
        ;;
    x86_64)
        asset=go2rtc_mac_amd64.zip
        expected_sha=$amd64_sha
        ;;
    *)
        printf 'unsupported platform: this Mac architecture has no pinned binary.\n' >&2
        exit 1
        ;;
esac

download_dir=$(mktemp -d "${TMPDIR:-/tmp}/kasa-go2rtc.XXXXXX")
install_dir=
cleanup() {
    rm -rf -- "$download_dir"
    if [ -n "$install_dir" ]; then
        rm -rf -- "$install_dir"
    fi
}
trap cleanup EXIT
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
# Stage on the same filesystem as the destination so the final rename is atomic.
install_dir=$(mktemp -d bin/.go2rtc.XXXXXX)
mv -- "$download_dir/go2rtc" "$install_dir/go2rtc"
mv -- "$install_dir/go2rtc" bin/go2rtc
printf 'Installed go2rtc v%s.\n' "$version"
