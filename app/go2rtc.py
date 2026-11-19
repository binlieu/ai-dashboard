"""Private Unix-socket go2rtc bridge, supervised without writable API calls."""

import copy
import http.client
import json
import os
from pathlib import Path
import re
import socket
import ssl
import stat
import subprocess
import tempfile
import threading
import time

from .discovery import valid_ip
from .envfile import b64, urlencode
from .security import CAMERA_ID
from .store import atomic_write

MAX_FRAME = 10 * 1024 * 1024


class Go2RTCError(RuntimeError):
    pass


def render_config(socket_path, cameras_with_ips):
    streams = []
    for camera in cameras_with_ips:
        if not camera.get("ip"):
            continue
        if not CAMERA_ID.fullmatch(camera["id"]) or not valid_ip(camera["ip"]):
            raise ValueError("Invalid camera stream")
        streams.append("  %s: kasa://${KASA_USER_ENC}:${KASA_PASS_B64}@%s:19443/https/stream/mixed?video=h264&audio=g711&resolution=hd" %
                       (camera["id"], camera["ip"]))
    return ('api:\n  listen: ""\n  unix_listen: %s\nrtsp:\n  listen: ""\nwebrtc:\n  listen: ""\n%s\nlog:\n  level: info\n' %
            (json.dumps(str(socket_path)), "streams:\n" + "\n".join(streams) if streams else "streams: {}"))


def choose_socket_path(repo):
    path = Path(repo).resolve() / "run" / "api.sock"
    if len(os.fsencode(path)) <= 100:
        return path, None
    directory = Path(tempfile.mkdtemp(prefix="kcd-", dir="/tmp"))
    os.chmod(directory, 0o700)
    return directory / "api.sock", directory


def remove_socket(path):
    try:
        mode = os.lstat(path).st_mode
    except FileNotFoundError:
        return
    if not stat.S_ISSOCK(mode):
        raise Go2RTCError("The private socket path is occupied by a non-socket file.")
    os.unlink(path)


class UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, path, timeout=15):
        super().__init__("localhost", timeout=timeout)
        self.path = str(path)

    def connect(self):
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            connection.settimeout(self.timeout)
            connection.connect(self.path)
        except Exception:
            connection.close()
            raise
        self.sock = connection


class UnixHTTPClient:
    def __init__(self, path, redact=None, connection_factory=UnixHTTPConnection):
        self.path = str(path)
        self.redact = redact
        self.connection_factory = connection_factory

    def ready(self, timeout=1):
        connection = self.connection_factory(self.path, timeout=timeout)
        try:
            connection.request("GET", "/api/streams")
            return connection.getresponse().status == 200
        except (OSError, http.client.HTTPException):
            return False
        finally:
            connection.close()

    def frame(self, camera_id):
        if not isinstance(camera_id, str) or not CAMERA_ID.fullmatch(camera_id):
            raise ValueError("Invalid camera ID")
        connection = self.connection_factory(self.path, timeout=15)
        try:
            connection.request("GET", "/api/frame.jpeg?src=" + camera_id)
            response = connection.getresponse()
            body = response.read(MAX_FRAME + 1)
            if len(body) > MAX_FRAME:
                raise Go2RTCError("Snapshot exceeds the size limit.")
            content_type = response.getheader("Content-Type", "application/octet-stream")
            if self.redact and (response.status != 200 or content_type.startswith("text/") or "json" in content_type):
                body = self.redact(body.decode("utf-8", errors="replace")).encode("utf-8")
            return response.status, content_type, body
        finally:
            connection.close()


def probe_camera(ip, user, password_b64):
    if not valid_ip(ip):
        return "unreachable"
    authorization = "Basic " + b64(user + ":" + password_b64)
    deadline = time.monotonic() + 6
    for legacy in (False, True):
        context = ssl._create_unverified_context()
        if legacy:
            try:
                context.minimum_version = ssl.TLSVersion.TLSv1
            except (AttributeError, ValueError, ssl.SSLError):
                pass
            try:
                context.set_ciphers("ALL:@SECLEVEL=0")
            except ssl.SSLError:
                try:
                    context.set_ciphers("ALL")
                except ssl.SSLError:
                    return "unreachable"
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return "unreachable"
        connection = None
        try:
            connection = http.client.HTTPSConnection(ip, 19443, timeout=remaining, context=context)
            connection.request("GET", "/https/stream/mixed?video=h264&audio=g711&resolution=hd",
                               headers={"Authorization": authorization})
            # Read only headers: consuming a live multipart body would never finish.
            status = connection.getresponse().status
            return {200: "ok", 503: "busy", 401: "auth"}.get(status, "unreachable")
        except ssl.SSLError:
            if legacy:
                return "unreachable"
        except (OSError, http.client.HTTPException, ValueError):
            return "unreachable"
        finally:
            if connection is not None:
                connection.close()
    return "unreachable"


class Go2RTC:
    def __init__(self, repo, user, password, popen=None, client=None, logger=print, ready_timeout=10):
        self.repo = Path(repo).resolve()
        self.config_path = self.repo / "run" / "go2rtc.yaml"
        self.config_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.config_path.parent, 0o700)
        self.socket_path, self._temporary_directory = choose_socket_path(self.repo)
        password_b64 = b64(password)
        self._environment = os.environ.copy()
        self._environment.pop("KASA_EMAIL", None)
        self._environment.pop("KASA_PASSWORD", None)
        self._environment.update(KASA_USER_ENC=urlencode(user), KASA_PASS_B64=urlencode(password_b64))
        self._secrets = sorted({user, password, password_b64, urlencode(user), urlencode(password_b64)} - {""},
                               key=len, reverse=True)
        self._popen = popen or subprocess.Popen
        self.client = client or UnixHTTPClient(self.socket_path, self.redact)
        self._logger = logger
        self._ready_timeout = min(10, ready_timeout)
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._process = None
        self._monitor = None
        self._output_threads = []
        self._cameras = []
        self._spawned_at = 0
        # Only an instance that spawned go2rtc may delete the socket; a second
        # start that fails on the port must not unlink the running one's socket.
        self._owns_socket = False

    def redact(self, text):
        text = re.sub(r"kasa://[^\s]*?@", "kasa://***@", text, flags=re.I)
        for secret in self._secrets:
            text = text.replace(secret, "***")
        return text

    def _forward_output(self, process):
        try:
            for line in iter(process.stdout.readline, b""):
                if not line:
                    break
                if isinstance(line, bytes):
                    line = line.decode("utf-8", errors="replace")
                self._logger(self.redact(line.rstrip("\r\n")))
        except (OSError, ValueError):
            pass
        finally:
            process.stdout.close()

    def _spawn_locked(self):
        # Never trust a config left by an earlier child, even after a crash.
        atomic_write(self.config_path, render_config(self.socket_path, self._cameras))
        remove_socket(self.socket_path)
        self._owns_socket = True
        self._process = self._popen(
            [str(self.repo / "bin" / "go2rtc"), "-c", "run/go2rtc.yaml"],
            cwd=str(self.repo), env=self._environment, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        self._spawned_at = time.monotonic()
        thread = threading.Thread(target=self._forward_output, args=(self._process,), daemon=True)
        self._output_threads = [item for item in self._output_threads if item.is_alive()]
        self._output_threads.append(thread)
        thread.start()

    def _wait_ready_locked(self):
        deadline = time.monotonic() + self._ready_timeout
        while not self._stop.is_set():
            remaining = deadline - time.monotonic()
            if remaining <= 0 or self._process.poll() is not None:
                break
            if self.client.ready(timeout=min(1, remaining)):
                return
            self._stop.wait(min(0.1, remaining))
        raise Go2RTCError("Stream bridge did not become ready in time.")

    def _terminate_locked(self):
        if self._process is not None:
            if self._process.poll() is None:
                self._process.terminate()
                try:
                    self._process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    self._process.kill()
                    self._process.wait(timeout=2)
            self._process = None

    def start(self, cameras_with_ips):
        with self._lock:
            if self._stop.is_set():
                raise Go2RTCError("Stream bridge has shut down.")
            self._cameras = copy.deepcopy(cameras_with_ips)
            try:
                self._spawn_locked()
                self._wait_ready_locked()
            except Exception:
                self._terminate_locked()
                raise Go2RTCError("Stream bridge could not start.") from None
            self._monitor = threading.Thread(target=self._supervise, daemon=True)
            self._monitor.start()

    def apply(self, cameras_with_ips):
        with self._lock:
            if self._stop.is_set():
                raise Go2RTCError("Stream bridge has shut down.")
            self._cameras = copy.deepcopy(cameras_with_ips)
            self._terminate_locked()
            try:
                self._spawn_locked()
                self._wait_ready_locked()
            except Exception:
                self._terminate_locked()
                raise Go2RTCError("Stream bridge could not apply camera changes.") from None

    def _supervise(self):
        failures = 0
        delays = (1, 2, 5, 10)
        while not self._stop.wait(0.2):
            with self._lock:
                alive = self._process is not None and self._process.poll() is None
                if alive and time.monotonic() - self._spawned_at >= 30:
                    failures = 0
            if alive:
                continue
            if self._stop.wait(delays[min(failures, len(delays) - 1)]):
                return
            with self._lock:
                if self._stop.is_set():
                    return
                if self._process is not None and self._process.poll() is None:
                    continue
                failures += 1
                try:
                    self._terminate_locked()
                    self._spawn_locked()
                    self._wait_ready_locked()
                except Exception:
                    self._terminate_locked()
                    self._logger("Stream bridge restart failed; retrying with backoff.")

    def frame(self, camera_id):
        return self.client.frame(camera_id)

    def open_mp4(self, camera_id):
        if not isinstance(camera_id, str) or not CAMERA_ID.fullmatch(camera_id):
            raise ValueError("Invalid camera ID")
        connection = self.client.connection_factory(self.client.path, timeout=20)
        try:
            connection.request("GET", "/api/stream.mp4?src=" + camera_id)
            transport = connection.sock
            response = connection.getresponse()
            if response.status != 200:
                response.close()
                raise Go2RTCError("The local stream bridge did not provide MP4 video.")
        except Exception:
            connection.close()
            raise

        class MP4Stream:
            read = response.read
            read1 = response.read1

            def interrupt(self):
                transport.shutdown(socket.SHUT_RDWR)

            def close(self):
                try:
                    response.close()
                finally:
                    connection.close()

        return MP4Stream()

    def open_socket(self):
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            connection.settimeout(5)
            connection.connect(str(self.socket_path))
            return connection
        except Exception:
            connection.close()
            raise

    def stop(self):
        self._stop.set()
        with self._lock:
            self._terminate_locked()
            if self._owns_socket:
                try:
                    remove_socket(self.socket_path)
                except Go2RTCError:
                    # A regular file replacing the socket is not ours to remove.
                    pass
            if self._temporary_directory is not None:
                try:
                    self._temporary_directory.rmdir()
                except OSError:
                    pass
                self._temporary_directory = None
        if self._monitor is not None:
            self._monitor.join(timeout=2)
        for thread in self._output_threads:
            thread.join(timeout=1)
