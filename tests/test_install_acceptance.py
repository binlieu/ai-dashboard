"""Acceptance tests for the Mac installer helpers (orchestrator-written; do not modify).

The installer writes the Kasa login to .env, registers login items, and moves settings between
Macs. These tests pin the parts that must not go wrong: the login survives intact and private,
nothing secret reaches the login items, and importing settings never loses the existing ones.
"""

import json
import os
from pathlib import Path
import plistlib
import stat
import tempfile
import unittest

from app.envfile import env_get
from app.install import (KIOSK_LABEL, SERVER_LABEL, InstallError, export_settings, import_settings,
                         protected_location, render_env, render_kiosk_agent, render_server_agent,
                         write_env)

CAMERAS = [{"id": "cam1", "mac": "aa:bb:cc:dd:ee:01", "name": "Front"}]
STATE = {"version": 1, "next_id": 2, "layout": "2x2", "cameras": CAMERAS,
         "devices": [{"id": "dev1", "mac": "aa:bb:cc:dd:ee:02", "name": "Porch"}], "next_device_id": 2,
         "weather": {"enabled": True, "place": "Springfield", "units": "f",
                     "name": "Springfield, Illinois, United States", "lat": 39.8, "lon": -89.64}}


class Env(unittest.TestCase):
    PASSWORDS = ["simple", "with space", "dollar$HOME`id`", "hash#tag=equals", "'single'", '"double"',
                 "mixed'\"quote", "back\\slash", "ünïcödé✓", " leading and trailing ", "a" * 200,
                 "'", '"', "=", "#"]

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / ".env"

    def test_every_login_reads_back_exactly(self):
        for password in self.PASSWORDS:
            self.path.write_text(render_env("user+tag@example.com", password), encoding="utf-8")
            self.assertEqual(env_get(self.path, "KASA_EMAIL"), "user+tag@example.com", password)
            self.assertEqual(env_get(self.path, "KASA_PASSWORD"), password, repr(password))
            self.assertIsNone(env_get(self.path, "DASHBOARD_PORT"))

    def test_port_is_optional_and_checked(self):
        self.path.write_text(render_env("user@example.com", "pw", port=1985), encoding="utf-8")
        self.assertEqual(env_get(self.path, "DASHBOARD_PORT"), "1985")
        for port in (0, 65536, -1, "1985", True, 1.5):
            with self.assertRaises(InstallError):
                render_env("user@example.com", "pw", port=port)

    def test_values_that_cannot_be_stored_are_refused(self):
        for email, password in (("", "pw"), ("user@example.com", ""), ("user@example.com", "two\nlines"),
                                ("user@example.com", "carriage\rreturn"), ("a\nb@example.com", "pw"),
                                (None, "pw"), ("user@example.com", None), ("user@example.com", "nul\x00")):
            with self.assertRaises(InstallError):
                render_env(email, password)

    def test_error_text_never_contains_the_password(self):
        try:
            render_env("user@example.com", "SECRET\nVALUE")
        except InstallError as error:
            self.assertNotIn("SECRET", str(error))

    def test_file_is_private_and_not_overwritten_by_accident(self):
        write_env(self.path, render_env("user@example.com", "first"))
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        with self.assertRaises(InstallError):
            write_env(self.path, render_env("user@example.com", "second"))
        self.assertEqual(env_get(self.path, "KASA_PASSWORD"), "first")
        write_env(self.path, render_env("user@example.com", "second"), replace=True)
        self.assertEqual(env_get(self.path, "KASA_PASSWORD"), "second")
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        self.assertEqual([entry.name for entry in self.path.parent.iterdir()], [".env"])


class Agents(unittest.TestCase):
    REPO = Path("/Users/example/kasa dashboard")

    def test_server_agent(self):
        agent = plistlib.loads(render_server_agent(self.REPO))
        self.assertEqual(SERVER_LABEL, "com.kasa-dashboard.server")
        self.assertEqual(agent["Label"], SERVER_LABEL)
        self.assertEqual(agent["ProgramArguments"], ["/bin/bash", str(self.REPO / "start.sh")])
        self.assertEqual(agent["WorkingDirectory"], str(self.REPO))
        self.assertIs(agent["RunAtLoad"], True)
        self.assertIs(agent["KeepAlive"], True)
        self.assertGreaterEqual(agent["ThrottleInterval"], 10)
        self.assertEqual(agent["StandardOutPath"], str(self.REPO / "run" / "dashboard.log"))
        self.assertEqual(agent["StandardErrorPath"], str(self.REPO / "run" / "dashboard.log"))

    def test_kiosk_agent(self):
        agent = plistlib.loads(render_kiosk_agent(self.REPO))
        self.assertEqual(KIOSK_LABEL, "com.kasa-dashboard.kiosk")
        self.assertEqual(agent["Label"], KIOSK_LABEL)
        self.assertEqual(agent["ProgramArguments"], ["/bin/bash", str(self.REPO / "scripts" / "kiosk.sh")])
        self.assertIs(agent["RunAtLoad"], True)
        self.assertNotIn("KeepAlive", agent)

    def test_nothing_secret_or_environment_specific_is_in_the_login_items(self):
        for data in (render_server_agent(self.REPO), render_kiosk_agent(self.REPO)):
            agent = plistlib.loads(data)
            self.assertNotIn("EnvironmentVariables", agent)
            text = data.decode("utf-8")
            for word in ("KASA", "PASSWORD", "EMAIL", "password"):
                self.assertNotIn(word, text)

    def test_relative_or_odd_paths_are_refused(self):
        for repo in ("relative/path", "", None, "/Users/example/new\nline"):
            with self.assertRaises(InstallError):
                render_server_agent(repo)


class ProtectedLocation(unittest.TestCase):
    def test_folders_macos_guards_from_background_programs(self):
        home = Path("/Users/example")
        for inside in ("Desktop/kasa", "Documents/kasa", "Downloads/ai-dashboard-master", "Desktop",
                       "Library/Mobile Documents/x"):
            self.assertTrue(protected_location(home / inside, home), inside)
        for fine in ("kasa-dashboard", "Applications/kasa", "Documentsx/kasa", "apps/Desktop-tools"):
            self.assertFalse(protected_location(home / fine, home), fine)
        self.assertFalse(protected_location(Path("/opt/kasa"), home))
        self.assertTrue(protected_location(Path("/Volumes/USB/kasa"), home))


class Settings(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.base = Path(directory.name)
        self.repo = self.base / "repo"
        (self.repo / "data").mkdir(parents=True)
        self.state_path = self.repo / "data" / "cameras.json"
        self.export_path = self.base / "kasa-dashboard-settings.json"

    def write_state(self, state):
        self.state_path.write_text(json.dumps(state), encoding="utf-8")

    def test_export_then_import_keeps_cameras_devices_layout_and_weather(self):
        self.write_state(STATE)
        export_settings(self.repo, self.export_path)
        self.assertEqual(stat.S_IMODE(self.export_path.stat().st_mode), 0o600)
        self.assertNotIn("KASA", self.export_path.read_text(encoding="utf-8"))
        target = self.base / "new-mac"
        result = import_settings(target, self.export_path)
        loaded = json.loads((target / "data" / "cameras.json").read_text(encoding="utf-8"))
        for key in ("cameras", "devices", "layout", "weather", "next_id", "next_device_id"):
            self.assertEqual(loaded[key], STATE[key], key)
        self.assertEqual(stat.S_IMODE((target / "data" / "cameras.json").stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE((target / "data").stat().st_mode), 0o700)
        self.assertEqual(result["cameras"], 1)
        self.assertEqual(result["devices"], 1)

    def test_backup_folder_is_kept_only_if_it_exists_on_this_mac(self):
        folder = self.base / "drive"
        folder.mkdir()
        self.write_state(dict(STATE, backup={"folder": str(folder), "cameras": {"cam1": 7}}))
        export_settings(self.repo, self.export_path)
        kept = import_settings(self.base / "a", self.export_path)
        self.assertIs(kept["backup_kept"], True)
        self.assertEqual(json.loads((self.base / "a/data/cameras.json").read_text())["backup"]["cameras"], {"cam1": 7})
        folder.rmdir()
        dropped = import_settings(self.base / "b", self.export_path)
        self.assertIs(dropped["backup_kept"], False)
        self.assertNotIn("cam1", json.dumps(json.loads((self.base / "b/data/cameras.json").read_text()).get("backup", {})))
        self.assertFalse(folder.exists())

    def test_import_keeps_a_copy_of_what_was_there(self):
        self.write_state(STATE)
        export_settings(self.repo, self.export_path)
        other = dict(STATE, layout="3x3")
        self.write_state(other)
        import_settings(self.repo, self.export_path)
        self.assertEqual(json.loads(self.state_path.read_text())["layout"], "2x2")
        copies = [path for path in self.state_path.parent.iterdir() if path.name.startswith("cameras.json.before-import-")]
        self.assertEqual(len(copies), 1)
        self.assertEqual(json.loads(copies[0].read_text())["layout"], "3x3")

    def test_bad_files_are_refused_and_change_nothing(self):
        self.write_state(STATE)
        before = self.state_path.read_bytes()
        bad = self.base / "bad.json"
        for content in ("", "{", "[]", json.dumps({"version": 2}), json.dumps(dict(STATE, cameras="x")),
                        json.dumps(dict(STATE, extra=1)), "x" * (2 * 1024 * 1024)):
            bad.write_text(content, encoding="utf-8")
            with self.assertRaises(InstallError, msg=content[:20]):
                import_settings(self.repo, bad)
            self.assertEqual(self.state_path.read_bytes(), before)
        with self.assertRaises(InstallError):
            import_settings(self.repo, self.base / "missing.json")
        with self.assertRaises(InstallError):
            export_settings(self.base / "no-such-repo", self.export_path)
        self.assertEqual(len(list(self.state_path.parent.iterdir())), 1)


if __name__ == "__main__":
    unittest.main()
