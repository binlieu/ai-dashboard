import contextlib
import io
import json
import os
from pathlib import Path
import plistlib
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, Mock, patch

from app import install
from app.envfile import env_get


STATE = {"version": 1, "next_id": 1, "layout": "auto", "cameras": []}
ROOT = Path(__file__).resolve().parents[1]
DOMAIN = f"gui/{os.getuid()}"


class InstallerTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.base = Path(directory.name).resolve()
        self.home = self.base / "home"
        self.repo = self.home / "kasa dashboard"
        self.repo.mkdir(parents=True)
        self.env = self.repo / ".env"
        self.settings = self.repo / install.SETTINGS_FILE
        self.agents = self.home / "Library/LaunchAgents"
        self.runner = Mock(side_effect=self.fake_runner)
        self.server_probe = Mock(return_value=True)
        self.return_codes = {}
        self.commands = []

    def fake_runner(self, command, **kwargs):
        self.commands.append(command)
        codes = self.return_codes.get(tuple(command), [])
        if codes:
            code = codes.pop(0)
        else:
            code = 1 if command[1] in ("print", "bootout") else 0
        return subprocess.CompletedProcess(command, code)

    def invoke(self, *args):
        output, errors = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors), \
                patch.object(install, "_server_answers", self.server_probe):
            status = install.main([*args, "--home", str(self.home)], repo=self.repo, runner=self.runner)
        return status, output.getvalue(), errors.getvalue()

    def set_login(self, port=None):
        install.write_env(self.env, install.render_env("user@example.com", "placeholder", port))

    def set_settings(self, state=STATE):
        self.settings.parent.mkdir(exist_ok=True)
        self.settings.write_text(json.dumps(state), encoding="utf-8")

    def launch_command(self, command, label):
        return ["/bin/launchctl", command, f"{DOMAIN}/{label}"]

    def bootstrap(self, label):
        return ["/bin/launchctl", "bootstrap", DOMAIN, str(self.agents / (label + ".plist"))]

    def test_fresh_install_writes_private_login_and_starts_both_agents(self):
        password = "literal $value with spaces and quotes '\""
        with patch("builtins.input", return_value="user@example.com"), \
                patch("app.install.getpass.getpass", return_value=password) as getpass:
            status, output, errors = self.invoke("install", "--port", "1985")

        self.assertEqual((status, errors), (0, ""))
        self.assertEqual(getpass.call_count, 2)
        self.assertNotIn(password, output + repr(self.runner.call_args_list))
        self.assertEqual(env_get(self.env, "KASA_PASSWORD"), password)
        self.assertEqual(env_get(self.env, "DASHBOARD_PORT"), "1985")
        self.assertEqual(stat.S_IMODE((self.repo / "run").stat().st_mode), 0o700)
        self.assertEqual(self.commands[0], ["/bin/bash", str(self.repo / "scripts/setup.sh")])
        for label in (install.SERVER_LABEL, install.KIOSK_LABEL):
            path = self.agents / (label + ".plist")
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o644)
            self.assertEqual(plistlib.loads(path.read_bytes())["Label"], label)
            self.assertIn(self.bootstrap(label), self.commands)
        self.server_probe.assert_called_once()
        self.assertEqual(self.server_probe.call_args.args, (1985,))

    def test_update_waits_for_old_server_and_retries_bootstrap(self):
        self.set_login(1987)
        self.set_settings()
        previous = self.env.read_bytes()
        kiosk = self.agents / (install.KIOSK_LABEL + ".plist")
        self.agents.mkdir(parents=True)
        kiosk.write_bytes(b"previous kiosk")
        server_print = self.launch_command("print", install.SERVER_LABEL)
        self.return_codes[tuple(server_print)] = [0, 1]
        server_bootstrap = self.bootstrap(install.SERVER_LABEL)
        self.return_codes[tuple(server_bootstrap)] = [1, 0]

        with patch("app.install.time.sleep") as sleep, patch("builtins.input") as prompt:
            status, output, errors = self.invoke("install", "--no-kiosk", "--port", "1990")

        self.assertEqual((status, errors), (0, ""))
        prompt.assert_not_called()
        self.assertEqual(self.env.read_bytes(), previous)
        self.assertEqual(json.loads(self.settings.read_bytes()), STATE)
        self.assertFalse(kiosk.exists())
        self.assertEqual(self.commands[1:], [
            self.launch_command("bootout", install.SERVER_LABEL), server_print, server_print,
            server_bootstrap, server_bootstrap, self.launch_command("bootout", install.KIOSK_LABEL),
            self.launch_command("print", install.KIOSK_LABEL),
        ])
        sleep.assert_any_call(3)
        self.assertIn("http://127.0.0.1:1987/", output)

    def test_no_autostart_only_runs_setup(self):
        self.set_login()
        status, output, errors = self.invoke("install", "--no-autostart")
        self.assertEqual((status, errors), (0, ""))
        self.assertIn("./start.sh", output)
        self.assertFalse(self.agents.exists())
        self.assertEqual(len(self.commands), 1)
        self.server_probe.assert_not_called()

    def test_every_dry_run_skips_commands_writes_http_and_prompts(self):
        self.repo = self.home / "Downloads/kasa-dashboard"
        cases = [("install", "--settings", "missing.json"), ("status",), ("login",),
                 ("uninstall",), ("import", "missing.json"), ("export", "out.json")]
        with patch("builtins.input") as prompt:
            for command in cases:
                with self.subTest(command=command):
                    status, output, errors = self.invoke(*command, "--dry-run")
                    self.assertEqual((status, errors), (0, ""))
                    self.assertIn("Would", output)
                    if command[0] == "install":
                        self.assertIn("Warning:", output)
            self.runner.assert_not_called()
            self.server_probe.assert_not_called()
            prompt.assert_not_called()
        self.assertFalse(self.repo.exists())
        self.assertFalse(self.agents.exists())

    def test_protected_install_refuses_before_running_setup(self):
        self.repo = self.home / "Desktop/kasa-dashboard"
        status, _, errors = self.invoke("install")
        self.assertEqual(status, 1)
        self.assertIn("~/kasa-dashboard", errors)
        self.runner.assert_not_called()

    def test_login_preserves_port_and_kickstarts_loaded_server(self):
        self.set_login(1990)
        server_print = self.launch_command("print", install.SERVER_LABEL)
        self.return_codes[tuple(server_print)] = [0]
        with patch("builtins.input", return_value="new@example.com"), \
                patch("app.install.getpass.getpass", return_value="new password"):
            status, output, errors = self.invoke("login")
        self.assertEqual((status, errors), (0, ""))
        self.assertNotIn("new password", output + repr(self.runner.call_args_list))
        self.assertEqual(env_get(self.env, "DASHBOARD_PORT"), "1990")
        self.assertEqual(env_get(self.env, "KASA_EMAIL"), "new@example.com")
        self.assertEqual(self.commands, [
            server_print,
            ["/bin/launchctl", "kickstart", "-k", f"{DOMAIN}/{install.SERVER_LABEL}"],
        ])

    def test_password_echo_fallback_leaves_existing_login_untouched(self):
        self.set_login()
        previous = self.env.read_bytes()
        with patch("builtins.input", return_value="user@example.com"), \
                patch("app.install.getpass.getpass", side_effect=install.getpass.GetPassWarning):
            status, _, errors = self.invoke("login")
        self.assertEqual(status, 1)
        self.assertIn("echo disabled", errors)
        self.assertEqual(self.env.read_bytes(), previous)
        self.runner.assert_not_called()

    def test_managed_import_boots_out_server_before_save_and_starts_it_after(self):
        self.set_settings()
        incoming = self.base / "settings.json"
        incoming.write_text(json.dumps(dict(STATE, layout="3x3")), encoding="utf-8")
        server_print = self.launch_command("print", install.SERVER_LABEL)
        self.return_codes[tuple(server_print)] = [0, 1]

        def run(command, **kwargs):
            layout = json.loads(self.settings.read_bytes())["layout"]
            if command == self.launch_command("bootout", install.SERVER_LABEL):
                self.assertEqual(layout, "auto")
            if command == self.bootstrap(install.SERVER_LABEL):
                self.assertEqual(layout, "3x3")
            return self.fake_runner(command, **kwargs)

        self.runner.side_effect = run
        status, _, errors = self.invoke("import", str(incoming))
        self.assertEqual((status, errors), (0, ""))
        self.assertEqual(self.commands, [server_print, self.launch_command("bootout", install.SERVER_LABEL),
                                         server_print, self.bootstrap(install.SERVER_LABEL)])

    def test_failed_import_rolls_back_backup_and_directory_permissions(self):
        self.set_settings()
        previous = self.settings.read_bytes()
        self.settings.parent.chmod(0o755)
        source = self.base / "settings.json"
        source.write_text(json.dumps(dict(STATE, layout="2x2")), encoding="utf-8")
        with patch("app.install.os.replace", side_effect=PermissionError):
            with self.assertRaises(install.InstallError):
                install.import_settings(self.repo, source)
        self.assertEqual(self.settings.read_bytes(), previous)
        self.assertEqual(stat.S_IMODE(self.settings.parent.stat().st_mode), 0o755)
        self.assertEqual(list(self.settings.parent.iterdir()), [self.settings])

    def test_nested_unknown_settings_and_duplicate_keys_are_rejected(self):
        source = self.base / "settings.json"
        for raw in (json.dumps(dict(STATE, weather={"enabled": False, "unknown": True})),
                    '{"version": 2, "version": 1, "next_id": 1, "layout": "auto", "cameras": []}'):
            source.write_text(raw, encoding="utf-8")
            with self.assertRaises(install.InstallError):
                install.import_settings(self.repo, source)
            self.assertFalse(self.settings.parent.exists())

    def test_uninstall_leaves_login_and_settings_in_place(self):
        self.set_login()
        self.set_settings()
        self.agents.mkdir(parents=True)
        for label in (install.SERVER_LABEL, install.KIOSK_LABEL):
            (self.agents / (label + ".plist")).touch()
        status, _, errors = self.invoke("uninstall")
        self.assertEqual((status, errors), (0, ""))
        self.assertEqual(list(self.agents.iterdir()), [])
        self.assertTrue(self.env.is_file())
        self.assertTrue(self.settings.is_file())

    def test_probe_uses_dashboard_path_and_explicit_host_port(self):
        connection = MagicMock()
        connection.getresponse.return_value.status = 200
        with patch("app.install.http.client.HTTPConnection", return_value=connection) as connect:
            self.assertTrue(install._server_answers(80))
        connect.assert_called_once_with("127.0.0.1", 80, timeout=3)
        connection.request.assert_called_once_with("GET", "/api/cameras", headers={"Host": "127.0.0.1:80"})
        connection.close.assert_called_once()

    @unittest.skipUnless(sys.platform == "darwin", "uses Apple's system Python")
    def test_kiosk_dry_run_reads_port_without_opening_the_browser(self):
        self.set_login(1988)
        (self.repo / "scripts").mkdir()
        (self.repo / "app").mkdir()
        for relative in ("scripts/kiosk.sh", "app/__init__.py", "app/envfile.py"):
            shutil.copyfile(ROOT / relative, self.repo / relative)
        result = subprocess.run(["/bin/bash", str(self.repo / "scripts/kiosk.sh"), "--dry-run"],
                                cwd=self.base, capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("http://127.0.0.1:1988/", result.stdout)
        self.assertIn("Browser command: /usr/bin/open ", result.stdout)
        self.assertNotIn("placeholder", result.stdout + result.stderr)
        self.assertFalse((self.repo / "app/__pycache__").exists())
