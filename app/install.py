"""Install the dashboard for the current Mac user and transfer its settings."""

import argparse
from contextlib import suppress
from datetime import datetime, timezone
import getpass
import http.client
import json
import os
from pathlib import Path
import plistlib
import stat
import subprocess
import sys
import tempfile
import time
import warnings

from .envfile import env_get
from .store import validate_state


SERVER_LABEL = "com.kasa-dashboard.server"
KIOSK_LABEL = "com.kasa-dashboard.kiosk"
DEFAULT_PORT = 1984
MAX_SETTINGS_BYTES = 1024 * 1024
SETTINGS_FILE = Path("data/cameras.json")
LOG_FILE = Path("run/dashboard.log")
LOCATION_ERROR = (
    "Move the dashboard folder directly into your home folder (for example, ~/kasa-dashboard) "
    "and run the installer again."
)
PORT_ERROR = "DASHBOARD_PORT must be a number from 1 to 65535."


class InstallError(Exception):
    """A fixed message safe to display in the installer."""


def render_env(email, password, port=None):
    for value in (email, password):
        if not isinstance(value, str) or not value or any(char in value for char in "\r\n\0"):
            raise InstallError("Enter a non-empty email and password without line breaks or NUL characters.")
        try:
            value.encode("utf-8")
        except UnicodeError:
            raise InstallError("The Kasa login must be valid UTF-8 text.") from None
    text = f'KASA_EMAIL="{email}"\nKASA_PASSWORD="{password}"\n'
    if port is not None:
        if type(port) is not int or not 1 <= port <= 65535:
            raise InstallError(PORT_ERROR)
        text += f"DASHBOARD_PORT={port}\n"
    return text


def _atomic_write(path, data, mode, *, replace):
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}-", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            os.fchmod(stream.fileno(), mode)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        if replace:
            os.replace(temporary, path)
        else:
            # Linking a complete file also protects against a simultaneous first write.
            os.link(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def write_env(path, text, replace=False):
    try:
        if not isinstance(text, str):
            raise TypeError
        _atomic_write(Path(path), text.encode("utf-8"), 0o600, replace=replace)
    except FileExistsError:
        raise InstallError("A .env file already exists. Use the login command to replace it.") from None
    except (OSError, ValueError, TypeError):
        raise InstallError("Could not save the private .env file.") from None


def _render_agent(repo, label, script, **options):
    try:
        repo = Path(repo)
        if not repo.is_absolute() or any(char in str(repo) for char in "\r\n\0"):
            raise ValueError
        return plistlib.dumps({
            "Label": label,
            "ProgramArguments": ["/bin/bash", str(repo / script)],
            "WorkingDirectory": str(repo),
            "RunAtLoad": True,
            **options,
        })
    except (ValueError, TypeError):
        raise InstallError("The dashboard folder must have a valid absolute path.") from None


def render_server_agent(repo):
    try:
        log = str(Path(repo) / LOG_FILE)
    except TypeError:
        raise InstallError("The dashboard folder must have a valid absolute path.") from None
    return _render_agent(repo, SERVER_LABEL, "start.sh", KeepAlive=True, ThrottleInterval=30,
                         StandardOutPath=log, StandardErrorPath=log)


def render_kiosk_agent(repo):
    return _render_agent(repo, KIOSK_LABEL, "scripts/kiosk.sh")


def protected_location(repo, home):
    home = Path(home).resolve()
    protected = (home / "Desktop", home / "Documents", home / "Downloads",
                 home / "Library/Mobile Documents", Path("/Volumes"))
    repo = Path(repo)
    return any(candidate.is_relative_to(folder) or candidate.is_relative_to(folder.resolve())
               for candidate in (repo.absolute(), repo.resolve()) for folder in protected)


def _unique_keys(pairs):
    result = dict(pairs)
    if len(result) != len(pairs):
        raise ValueError
    return result


def _read_settings(path):
    try:
        if not path.is_file():
            raise OSError
        with path.open("rb") as stream:
            if os.fstat(stream.fileno()).st_size > MAX_SETTINGS_BYTES:
                raise InstallError("The settings file exceeds the 1 MiB limit.")
            raw = stream.read(MAX_SETTINGS_BYTES)
            if os.fstat(stream.fileno()).st_size > MAX_SETTINGS_BYTES:
                raise InstallError("The settings file exceeds the 1 MiB limit.")
    except OSError:
        raise InstallError("Could not read the dashboard settings file.") from None

    try:
        state = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_keys)
        resets = []
        if validate_state(state, warning=resets.append) != state or resets:
            raise ValueError
    except (ValueError, TypeError, OverflowError, RecursionError):
        raise InstallError("The file does not contain valid dashboard settings.") from None
    return raw, state


def export_settings(repo, destination):
    try:
        repo, destination = Path(repo), Path(destination)
        source = repo / SETTINGS_FILE
        if destination.resolve() in ((repo / ".env").resolve(), source.resolve()):
            raise InstallError("Choose a separate file for the settings export.")
        raw, _ = _read_settings(source)
        _atomic_write(destination, raw, 0o600, replace=True)
    except (OSError, ValueError, TypeError, RuntimeError):
        raise InstallError("Could not save the settings export.") from None


def _prepare_import(source):
    try:
        _, state = _read_settings(Path(source))
        folder = state.get("backup", {}).get("folder")
        try:
            keep_backup = bool(folder and Path(folder).is_dir())
        except OSError:
            keep_backup = False
    except (OSError, ValueError, TypeError):
        raise InstallError("Could not open the settings to import.") from None
    if not keep_backup and "backup" in state:
        state["backup"] = {"folder": "", "cameras": {}}
    return state, {"cameras": len(state["cameras"]), "devices": len(state.get("devices", [])),
                   "backup_kept": keep_backup}


def _save_import(repo, state):
    path = repo / SETTINGS_FILE
    created = []
    original_mode = None
    backup = None
    try:
        missing = []
        directory = path.parent
        while not os.path.lexists(directory):
            missing.append(directory)
            directory = directory.parent
        for directory in reversed(missing):
            directory.mkdir(mode=0o700)
            created.append(directory)

        mode = path.parent.lstat().st_mode
        if not stat.S_ISDIR(mode):
            raise OSError
        if stat.S_IMODE(mode) != 0o700:
            path.parent.chmod(0o700)
            original_mode = stat.S_IMODE(mode)

        if os.path.lexists(path):
            if not stat.S_ISREG(path.lstat().st_mode):
                raise OSError
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
            backup_path = path.with_name(f"{path.name}.before-import-{stamp}")
            _atomic_write(backup_path, path.read_bytes(), 0o600, replace=False)
            backup = backup_path

        data = (json.dumps(state, indent=2) + "\n").encode("utf-8")
        _atomic_write(path, data, 0o600, replace=True)
    except (OSError, ValueError):
        if backup is not None:
            with suppress(OSError):
                backup.unlink()
        if original_mode is not None:
            with suppress(OSError):
                path.parent.chmod(original_mode)
        for directory in reversed(created):
            with suppress(OSError):
                directory.rmdir()
        raise InstallError("Could not save the imported dashboard settings.") from None


def import_settings(repo, source):
    state, result = _prepare_import(source)
    try:
        _save_import(Path(repo), state)
    except (ValueError, TypeError):
        raise InstallError("Could not save the imported dashboard settings.") from None
    return result


class LaunchAgents:
    def __init__(self, home, runner):
        self.directory = home / "Library/LaunchAgents"
        self.domain = f"gui/{os.getuid()}"
        self.runner = runner

    def path(self, label):
        return self.directory / f"{label}.plist"

    def _run(self, command, *arguments, timeout=5):
        try:
            return self.runner(["/bin/launchctl", command, *arguments], stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, timeout=timeout).returncode
        except (OSError, subprocess.SubprocessError):
            raise InstallError("Could not contact launchd. Run the installer from your Mac desktop session.") from None

    def loaded(self, label, timeout=5):
        return self._run("print", f"{self.domain}/{label}", timeout=timeout) == 0

    def stop(self, label):
        self._run("bootout", f"{self.domain}/{label}", timeout=20)
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if not self.loaded(label, timeout=max(0.01, min(5, deadline - time.monotonic()))):
                return
            time.sleep(max(0, min(0.5, deadline - time.monotonic())))
        raise InstallError("A dashboard login agent did not stop within 20 seconds. Try again.")

    def bootstrap(self, label):
        for attempt in range(2):
            if attempt:
                time.sleep(3)
            if self._run("bootstrap", self.domain, str(self.path(label))) == 0:
                return
        raise InstallError("Could not load a dashboard login agent. Run the installer again from your Mac desktop session.")

    def restart_server(self):
        if self.loaded(SERVER_LABEL):
            if self._run("kickstart", "-k", f"{self.domain}/{SERVER_LABEL}") != 0:
                raise InstallError("The Kasa login was saved, but the dashboard server could not be restarted.")

    def write(self, label, data):
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            _atomic_write(self.path(label), data, 0o644, replace=True)
        except OSError:
            raise InstallError("Could not write the dashboard login agent in your Library folder.") from None

    def remove(self, label):
        self.stop(label)
        try:
            self.path(label).unlink(missing_ok=True)
        except OSError:
            raise InstallError("Could not remove the dashboard login agent from your Library folder.") from None


def _port_argument(text):
    if not text.isascii() or not text.isdecimal() or len(text) > 5 or not 1 <= int(text) <= 65535:
        raise argparse.ArgumentTypeError(PORT_ERROR)
    return int(text)


def _configured_port(path):
    try:
        value = env_get(path, "DASHBOARD_PORT")
        return _port_argument(value) if value else None
    except argparse.ArgumentTypeError:
        raise InstallError(PORT_ERROR) from None
    except (OSError, UnicodeError):
        raise InstallError("Could not read the dashboard port from .env.") from None


def _prompt_login(port):
    try:
        email = input("Kasa email: ")
        with warnings.catch_warnings():
            # Prevent getpass from falling back to an input that echoes the password.
            warnings.simplefilter("error", getpass.GetPassWarning)
            password = getpass.getpass("Kasa password: ")
            confirmation = getpass.getpass("Confirm Kasa password: ")
    except (EOFError, OSError, getpass.GetPassWarning):
        raise InstallError("Open Terminal to enter your Kasa login with password echo disabled.") from None
    if password != confirmation:
        raise InstallError("The passwords did not match. Run the command again.")
    return render_env(email, password, port)


def _server_answers(port, timeout=3):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        connection.request("GET", "/api/cameras", headers={"Host": f"127.0.0.1:{port}"})
        return connection.getresponse().status == 200
    except (OSError, http.client.HTTPException):
        return False
    finally:
        connection.close()


def _wait_for_server(port):
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        if _server_answers(port, timeout=max(0.01, min(3, deadline - time.monotonic()))):
            return True
        time.sleep(max(0, min(1, deadline - time.monotonic())))
    return False


def _print_import(result):
    print(f"Imported {result['cameras']} camera(s) and {result['devices']} plug(s) or switch(es).")
    if not result["backup_kept"]:
        print("Recording backups are off. You can select a folder in Setup → Backup.")


def _install(args, repo, agents, runner):
    if not args.allow_location and protected_location(repo, args.home):
        if not args.dry_run:
            raise InstallError(LOCATION_ERROR)
        print(f"Warning: {LOCATION_ERROR}")
    env_path = repo / ".env"
    existing_env = os.path.lexists(env_path)
    port = (_configured_port(env_path) if existing_env else args.port) or DEFAULT_PORT
    url = f"http://127.0.0.1:{port}/"
    if args.dry_run:
        print("Would run scripts/setup.sh to install the pinned go2rtc bridge.")
        print("Would keep the existing .env." if existing_env else "Would prompt for the Kasa login and save it in .env.")
        print(f"Would import settings from {args.settings}." if args.settings else "Would keep existing settings.")
        if args.no_autostart:
            print("Would skip login agent setup. Start the dashboard with ./start.sh.")
        else:
            print(f"Would create the log folder and reload the server login agent in {agents.directory}.")
            print("Would remove the kiosk login agent." if args.no_kiosk else "Would reload the kiosk login agent.")
            print(f"Would wait up to 60 seconds for {url}api/cameras.")
        print(f"Dashboard address: {url}\nServer log: {repo / LOG_FILE}")
        return 0

    try:
        if runner(["/bin/bash", str(repo / "scripts/setup.sh")], cwd=repo).returncode != 0:
            raise InstallError("Could not install the pinned go2rtc bridge. Check the setup output and try again.")
    except (OSError, subprocess.SubprocessError):
        raise InstallError("Could not run the go2rtc setup script.") from None
    if existing_env:
        print("Keeping the existing .env and its dashboard port.")
    else:
        write_env(env_path, _prompt_login(args.port))

    server_stopped = False
    if args.settings:
        state, result = _prepare_import(args.settings)
        if not args.no_autostart and agents.loaded(SERVER_LABEL):
            agents.stop(SERVER_LABEL)
            server_stopped = True
        try:
            _save_import(repo, state)
        except InstallError:
            if server_stopped:
                agents.bootstrap(SERVER_LABEL)
            raise
        _print_import(result)

    ready = True
    if args.no_autostart:
        print("Start the dashboard with ./start.sh.")
    else:
        log_directory = (repo / LOG_FILE).parent
        try:
            log_directory.mkdir(mode=0o700, exist_ok=True)
            if log_directory.is_symlink():
                raise OSError
            agents.write(SERVER_LABEL, render_server_agent(repo))
            if not args.no_kiosk:
                agents.write(KIOSK_LABEL, render_kiosk_agent(repo))
        except (InstallError, OSError) as error:
            if server_stopped:
                agents.bootstrap(SERVER_LABEL)
            if isinstance(error, OSError):
                raise InstallError("Could not create the dashboard log folder.") from None
            raise
        if not server_stopped:
            agents.stop(SERVER_LABEL)
        agents.bootstrap(SERVER_LABEL)
        if args.no_kiosk:
            agents.remove(KIOSK_LABEL)
        else:
            agents.stop(KIOSK_LABEL)
            agents.bootstrap(KIOSK_LABEL)
        ready = _wait_for_server(port)
        if not ready:
            print("The login agents were configured, but the dashboard did not answer within 60 seconds.")
    print(f"Dashboard address: {url}\nServer log: {repo / LOG_FILE}")
    print("See docs/INSTALL-MAC.md for automatic login, restart after power failure, and first-run permissions.")
    print("Only one computer may run the dashboard for the same cameras. Stop it on the old Mac.")
    return 0 if ready else 1


def _run_command(args, repo, agents):
    command = args.command
    if args.dry_run:
        plan = {
            "uninstall": "Would stop and remove both login agents and keep the project, .env and settings.",
            "login": "Would prompt for the Kasa login, replace .env, and restart the server if loaded.",
            "export": "Would validate and export dashboard settings to",
            "import": "Would validate and import dashboard settings from",
            "status": "Would check the login agents, the local server, and the saved camera and device counts.",
        }
        message = plan[command]
        if command in ("export", "import"):
            message += f" {args.file}."
        print(message)
        return 0

    if command == "uninstall":
        for label in (KIOSK_LABEL, SERVER_LABEL):
            agents.remove(label)
        print("Dashboard login agents removed. The project folder, .env and settings are still in place.")
    elif command == "login":
        path = repo / ".env"
        write_env(path, _prompt_login(_configured_port(path)), replace=True)
        agents.restart_server()
        print("Saved the Kasa login in .env.")
    elif command == "export":
        export_settings(repo, args.file)
        print("Exported dashboard settings.")
    elif command == "import":
        state, result = _prepare_import(args.file)
        loaded = agents.loaded(SERVER_LABEL)
        if loaded:
            agents.stop(SERVER_LABEL)
        try:
            _save_import(repo, state)
        finally:
            if loaded:
                agents.bootstrap(SERVER_LABEL)
        _print_import(result)
    elif command == "status":
        for name, label in (("Server", SERVER_LABEL), ("Kiosk", KIOSK_LABEL)):
            print(f"{name} login agent: {'loaded' if agents.loaded(label) else 'not loaded'}")
        port = _configured_port(repo / ".env") or DEFAULT_PORT
        print(f"Server at http://127.0.0.1:{port}/: {'answering' if _server_answers(port) else 'not answering'}")
        path = repo / SETTINGS_FILE
        state = _read_settings(path)[1] if path.exists() else {"cameras": []}
        print(f"Saved cameras: {len(state['cameras'])}. Saved plugs and switches: {len(state.get('devices', []))}.")
    return 0


def main(argv=None, *, repo=None, runner=subprocess.run):
    parser = argparse.ArgumentParser(description="Configure the Kasa dashboard on this Mac.")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("install", "uninstall", "login", "export", "import", "status"):
        command = commands.add_parser(name)
        command.add_argument("--dry-run", action="store_true", help="print the plan without making changes")
        command.add_argument("--home", type=Path, default=Path.home(), help="override the home folder")
        if name in ("export", "import"):
            command.add_argument("file", type=Path)
        if name == "install":
            command.add_argument("--settings", type=Path, metavar="FILE")
            command.add_argument("--port", type=_port_argument, metavar="N", help="port for a new .env")
            command.add_argument("--no-autostart", action="store_true")
            command.add_argument("--no-kiosk", action="store_true")
            command.add_argument("--allow-location", action="store_true", help=argparse.SUPPRESS)

    args = parser.parse_args(argv)
    try:
        repo = Path(repo).resolve() if repo is not None else Path(__file__).resolve().parent.parent
        agents = LaunchAgents(args.home, runner)
        if args.command == "install":
            return _install(args, repo, agents, runner)
        return _run_command(args, repo, agents)
    except InstallError as error:
        print(error, file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nDashboard command cancelled.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
