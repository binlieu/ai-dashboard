"""Entry point; secrets are read only here and never used in process arguments."""

import argparse
import errno
import os
from pathlib import Path
import signal
import sys
import threading
import webbrowser

from .backup import BackupService
from .discovery import resolve_cameras, sweep_local_network
from .devices import DeviceService
from .envfile import b64, env_get
from .go2rtc import Go2RTC, Go2RTCError
from .kasa_control import DeviceClient
from .server import DashboardApplication, DashboardServer
from .store import CameraStore, StoreError
from .weather import WeatherService


def main(argv=None, repo=None):
    parser = argparse.ArgumentParser(description="Local Kasa camera dashboard")
    parser.add_argument("--open", action="store_true", help="open the dashboard in your browser")
    arguments = parser.parse_args(argv)
    repo = Path(repo).resolve() if repo is not None else Path(__file__).resolve().parent.parent
    os.chdir(repo)
    bridge = server = application = thread = weather = backup = None
    stopped = threading.Event()
    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, lambda number, frame: stopped.set())
    try:
        path = repo / ".env"
        if not path.is_file():
            print("Missing .env. Copy .env.example to .env and fill in the Kasa login.", file=sys.stderr)
            return 1
        user = env_get(path, "KASA_EMAIL")
        password = env_get(path, "KASA_PASSWORD")
        for key, value in (("KASA_EMAIL", user), ("KASA_PASSWORD", password)):
            if not value:
                print("Missing required key: %s." % key, file=sys.stderr)
                return 1
        port_text = env_get(path, "DASHBOARD_PORT") or "1984"
        if not port_text.isascii() or not port_text.isdecimal() or len(port_text) > 5 or not 1 <= int(port_text) <= 65535:
            print("DASHBOARD_PORT must be a number from 1 to 65535.", file=sys.stderr)
            return 1
        port = int(port_text)
        store = CameraStore(repo / "data" / "cameras.json", seed=lambda: {
            key: env_get(path, key) for key in ("CAMERA_MACS", "CAMERA_NAMES", "CAMERA_MAC")},
            warning=lambda text: print(text, file=sys.stderr))
        bridge = Go2RTC(repo, user, password)
        weather = WeatherService(store)
        devices = DeviceService(store, DeviceClient(user, password))
        backup = BackupService(store, bridge.open_mp4, (repo,))
        application = DashboardApplication(store, bridge, user, b64(password),
                                           weather=weather, devices=devices, backup=backup)
        # Reserve the public port before any camera connection is opened.
        server = DashboardServer(("127.0.0.1", port), application, repo / "www")
        cameras = resolve_cameras(store.snapshot()["cameras"], sweep=sweep_local_network)
        application.ips = {camera["id"]: camera["ip"] for camera in cameras}
        if stopped.is_set():
            return 0
        bridge.start(cameras)
        backup.start()
        application.start_background()
        weather.start()
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        url = "http://127.0.0.1:%s/" % port
        print("Dashboard: " + url, flush=True)
        for camera in cameras:
            print('Camera "%s": %s' % (camera["name"], camera["ip"] or "not found"), flush=True)
        if arguments.open:
            webbrowser.open(url)
        stopped.wait()
        return 0
    except StoreError as error:
        print(error.message, file=sys.stderr)
        return 1
    except OSError as error:
        print("Dashboard port is already in use." if error.errno == errno.EADDRINUSE
              else "Dashboard could not start. Check local file permissions and the go2rtc installation.", file=sys.stderr)
        return 1
    except (ValueError, Go2RTCError):
        print("Dashboard could not start. Check configuration and the stream bridge.", file=sys.stderr)
        return 1
    finally:
        if weather is not None:
            weather.stop()
        if server is not None:
            if thread is not None:
                server.shutdown()
            server.server_close()
        if backup is not None:
            backup.stop()
        if application is not None:
            application.stop_background()
        if bridge is not None:
            bridge.stop()


if __name__ == "__main__":
    sys.exit(main())
