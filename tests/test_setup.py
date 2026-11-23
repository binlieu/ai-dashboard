import hashlib
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
import zipfile


SETUP = Path(__file__).resolve().parents[1] / "scripts" / "setup.sh"
RELEASES = (
    ("12.7.6", "arm64", "1.9.14", "arm64", "919b78adc759d6b3883d1e1b2ac915ac0985bb903ff1897b4d228527bd64690c"),
    ("12.7.6", "x86_64", "1.9.14", "amd64", "9b0b9a27a4dc3a5b8b93376e7e8fc2787c6af624a512842622be84aec0171c7a"),
    ("13.0", "arm64", "1.9.15", "arm64", "d0cf858a71803dce2377423bc830639137ee96c335f88e9912e7f7898374266d"),
    ("26.1", "x86_64", "1.9.15", "amd64", "7ca6075421b824eebe0383d20bd42a6f6a7ec7591a60931d42290f1d375169b9"),
)


class BridgeSetupFixture:
    binary_contents = b"#!/bin/bash\nprintf 'downloaded go2rtc fixture\\n'\n"

    def __init__(self, base):
        self.commands = base / "commands"
        self.temp_downloads = base / "temp"
        self.requests = base / "requests"
        self.repo = base / "dashboard with spaces"
        self.setup = self.repo / "scripts/setup.sh"
        self.binary = self.repo / "bin/go2rtc"
        self.archive = base / "fixture.zip"
        self.commands.mkdir()
        self.temp_downloads.mkdir()
        self.setup.parent.mkdir(parents=True)
        self.setup.write_bytes(SETUP.read_bytes())
        with zipfile.ZipFile(self.archive, "w") as archive:
            archive.writestr("go2rtc", self.binary_contents)

        self.write_executable(self.commands / "sw_vers", """\
test "$1" = -productVersion
test "${SETUP_TEST_VERSION+x}" = x
printf '%s\\n' "$SETUP_TEST_VERSION"
""")
        self.write_executable(self.commands / "uname", """\
case "$1" in
    -s) printf 'Darwin\\n' ;;
    -m) printf '%s\\n' "$SETUP_TEST_ARCH" ;;
    *) exit 2 ;;
esac
""")
        self.write_executable(self.commands / "curl", """\
while [ "$1" != --output ]; do shift; done
printf '%s\\n' "$3" >> "$SETUP_TEST_REQUESTS"
cp -- "$SETUP_TEST_ARCHIVE" "$2"
""")

    @staticmethod
    def write_executable(path, script):
        path.write_text("#!/bin/bash\nset -euo pipefail\n" + script, encoding="utf-8")
        path.chmod(0o755)

    def pin_fixture_archive(self, release_checksum):
        # Rewrite only the expected release's pin in the test copy. The other
        # pins stay real so selecting the wrong digest fails the real SHA check.
        checksum = hashlib.sha256(self.archive.read_bytes()).hexdigest()
        source = self.setup.read_text(encoding="utf-8")
        self.setup.write_text(source.replace(release_checksum, checksum, 1), encoding="utf-8")

    def seed_binary(self, version):
        self.binary.parent.mkdir(exist_ok=True)
        self.write_executable(self.binary, f"printf 'go2rtc version {version} darwin/fixture\\n'\n")

    def run(self, version, architecture):
        env = {
            "PATH": f"{self.commands}:/usr/bin:/bin",
            "TMPDIR": str(self.temp_downloads),
            "SETUP_TEST_ARCH": architecture,
            "SETUP_TEST_ARCHIVE": str(self.archive),
            "SETUP_TEST_REQUESTS": str(self.requests),
        }
        if version is not None:
            env["SETUP_TEST_VERSION"] = version
        return subprocess.run(["/bin/bash", str(self.setup)], cwd=self.repo.parent, env=env,
                              capture_output=True, text=True, timeout=10)


class SetupTests(unittest.TestCase):
    def test_selects_release_url_and_checksum_for_each_supported_pair(self):
        for macos, architecture, release, asset_arch, checksum in RELEASES:
            with self.subTest(macos=macos, architecture=architecture), tempfile.TemporaryDirectory() as base:
                fixture = BridgeSetupFixture(Path(base))
                fixture.pin_fixture_archive(checksum)
                result = fixture.run(macos, architecture)

                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(fixture.requests.read_text(encoding="utf-8").splitlines(), [
                    f"https://github.com/AlexxIT/go2rtc/releases/download/v{release}/go2rtc_mac_{asset_arch}.zip",
                ])
                self.assertEqual(result.stdout.splitlines(), [
                    f"Downloading go2rtc v{release}...", f"Installed go2rtc v{release}.",
                ])
                self.assertEqual(fixture.binary.read_bytes(), fixture.binary_contents)
                self.assertTrue(os.access(fixture.binary, os.X_OK))
                self.assertEqual(list(fixture.binary.parent.iterdir()), [fixture.binary])
                self.assertEqual(list(fixture.temp_downloads.iterdir()), [])

    def test_checksum_mismatch_installs_nothing_and_cleans_up(self):
        with tempfile.TemporaryDirectory() as base:
            fixture = BridgeSetupFixture(Path(base))
            # An invalid archive must fail at the checksum check before unzip runs.
            fixture.archive.write_bytes(b"invalid zip fixture")
            result = fixture.run("12.7.6", "x86_64")

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("checksum mismatch", result.stderr)
            self.assertNotIn("Installed", result.stdout)
            self.assertTrue(fixture.requests.exists())
            self.assertFalse(fixture.binary.parent.exists())
            self.assertEqual(list(fixture.temp_downloads.iterdir()), [])

    def test_checksum_mismatch_leaves_an_existing_binary_in_place(self):
        with tempfile.TemporaryDirectory() as base:
            fixture = BridgeSetupFixture(Path(base))
            fixture.seed_binary("1.9.15")
            original = fixture.binary.read_bytes()
            result = fixture.run("12.7.6", "x86_64")

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("checksum mismatch", result.stderr)
            self.assertEqual(fixture.binary.read_bytes(), original)
            self.assertEqual(list(fixture.temp_downloads.iterdir()), [])

    def test_unsupported_or_unreadable_macos_version_stops_before_download(self):
        for macos in ("11.7.10", "garbage", "12.garbage", "", None):
            with self.subTest(macos=macos), tempfile.TemporaryDirectory() as base:
                fixture = BridgeSetupFixture(Path(base))
                fixture.seed_binary("1.9.15")
                result = fixture.run(macos, "x86_64")

                self.assertEqual(result.returncode, 1)
                self.assertEqual(result.stderr, "unsupported platform: macOS 12 or newer is required.\n")
                self.assertFalse(fixture.requests.exists())
                self.assertEqual(list(fixture.temp_downloads.iterdir()), [])

    def test_shortcut_accepts_only_the_selected_release(self):
        for macos, architecture, release, _, checksum in (RELEASES[1], RELEASES[2]):
            for installed in ("1.9.14", "1.9.15", release + "0", release.replace(".", "x")):
                with self.subTest(macos=macos, installed=installed), tempfile.TemporaryDirectory() as base:
                    fixture = BridgeSetupFixture(Path(base))
                    fixture.seed_binary(installed)
                    original = fixture.binary.read_bytes()
                    fixture.pin_fixture_archive(checksum)
                    result = fixture.run(macos, architecture)

                    self.assertEqual(result.returncode, 0, result.stderr)
                    if installed == release:
                        self.assertFalse(fixture.requests.exists())
                        self.assertEqual(fixture.binary.read_bytes(), original)
                        self.assertEqual(result.stdout, "")
                    else:
                        self.assertTrue(fixture.requests.exists())
                        self.assertEqual(fixture.binary.read_bytes(), fixture.binary_contents)
