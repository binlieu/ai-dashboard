"""Same-origin HTTP facade. No arbitrary path is ever proxied to go2rtc."""

import base64
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import re
import select
import socket
import threading
from urllib.parse import unquote, urlsplit

from .discovery import ip_for_mac, sweep_local_network, valid_ip
from .go2rtc import MAX_FRAME, probe_camera
from .security import check_request
from .store import MAX_CAMERAS, StoreError
from .weather import WeatherError, WeatherService

CONTENT_TYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
                 ".css": "text/css; charset=utf-8", ".json": "application/json; charset=utf-8",
                 ".svg": "image/svg+xml", ".png": "image/png", ".ico": "image/x-icon"}


class DashboardApplication:
    def __init__(self, store, bridge, user, password_b64, lookup=ip_for_mac,
                 sweep=sweep_local_network, probe=probe_camera, initial_cameras=None, weather=None):
        self.store, self.bridge = store, bridge
        self.weather = weather if weather is not None else WeatherService(store)
        self._user, self._password_b64 = user, password_b64
        self.lookup, self.sweep, self.probe = lookup, sweep, probe
        self.lock = threading.RLock()
        self.ips = {camera["id"]: valid_ip(camera.get("ip")) for camera in (initial_cameras or [])}
        self._stop = threading.Event()
        self._refresh_thread = None

    def camera_list(self):
        with self.lock:
            cameras = self.store.snapshot()["cameras"]
            return [dict(camera, ip=self.ips.get(camera["id"]),
                         available=bool(self.ips.get(camera["id"]))) for camera in cameras]

    def camera(self, camera_id):
        with self.lock:
            for camera in self.camera_list():
                if camera["id"] == camera_id:
                    return camera
            raise StoreError("not_found", "Camera not found.", 404)

    def metadata(self):
        with self.lock:
            return {"layout": self.store.snapshot()["layout"], "max": MAX_CAMERAS,
                    "cameras": self.camera_list()}

    def _apply_or_restore(self, previous, previous_ips):
        try:
            self.bridge.apply(self.camera_list())
        except Exception:
            self.store.restore(previous)
            self.ips = previous_ips
            try:
                self.bridge.apply(self.camera_list())
            except Exception:
                # Supervision will continue retrying the restored desired configuration.
                pass
            raise StoreError("go2rtc", "Stream bridge could not apply the change; camera settings were restored.", 502) from None

    def add(self, body):
        with self.lock:
            candidate = self.store.check_add(body.get("mac"), body.get("name"))
            ip = valid_ip(self.lookup(candidate["mac"]))
            if ip is None:
                self.sweep()
                ip = valid_ip(self.lookup(candidate["mac"]))
            if ip is None:
                raise StoreError("not_found", "Camera not found on this network.", 404)
            result = self.probe(ip, self._user, self._password_b64)
            if result == "auth":
                raise StoreError("auth", "The camera rejected the Kasa account login.", 422)
            if result not in ("ok", "busy"):
                raise StoreError("not_kasa", "The address did not answer as a Kasa camera.", 422)
            previous, previous_ips = self.store.snapshot(), self.ips.copy()
            camera = self.store.add(candidate["mac"], candidate["name"])
            self.ips[camera["id"]] = ip
            self._apply_or_restore(previous, previous_ips)
            return self.camera(camera["id"])

    def rename(self, camera_id, body):
        with self.lock:
            self.store.rename(camera_id, body.get("name"))
            return self.camera(camera_id)

    def remove(self, camera_id):
        with self.lock:
            previous, previous_ips = self.store.snapshot(), self.ips.copy()
            self.store.remove(camera_id)
            self.ips.pop(camera_id, None)
            self._apply_or_restore(previous, previous_ips)

    def set_layout(self, body):
        with self.lock:
            return {"layout": self.store.set_layout(body.get("layout"))}

    def refresh_once(self):
        with self.lock:
            changed = False
            for camera in self.store.snapshot()["cameras"]:
                ip = valid_ip(self.lookup(camera["mac"]))
                # An expired ARP entry alone does not invalidate an active stream.
                if ip and self.ips.get(camera["id"]) != ip:
                    self.ips[camera["id"]] = ip
                    changed = True
            if changed:
                try:
                    self.bridge.apply(self.camera_list())
                except Exception:
                    # The desired addresses remain current; child supervision retries.
                    pass

    def start_background(self):
        def refresh():
            while not self._stop.wait(60):
                try:
                    self.refresh_once()
                except Exception:
                    pass
        self._refresh_thread = threading.Thread(target=refresh, daemon=True)
        self._refresh_thread.start()

    def stop_background(self):
        self._stop.set()
        if self._refresh_thread is not None:
            self._refresh_thread.join(timeout=5)


class DashboardServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, application, www, relay_limit=64, bind_and_activate=True):
        if address[0] != "127.0.0.1":
            raise ValueError("Dashboard must bind to 127.0.0.1")
        self.application = application
        self.www = os.path.realpath(www)
        self.relay_slots = threading.BoundedSemaphore(relay_limit)
        self._relay_lock = threading.Lock()
        self._relay_sockets = set()
        super().__init__(address, DashboardHandler, bind_and_activate=bind_and_activate)

    def track_relay(self, *connections):
        with self._relay_lock:
            self._relay_sockets.update(connections)

    def untrack_relay(self, *connections):
        with self._relay_lock:
            self._relay_sockets.difference_update(connections)

    def server_close(self):
        with self._relay_lock:
            for connection in self._relay_sockets:
                try:
                    connection.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                connection.close()
            self._relay_sockets.clear()
        super().server_close()

    def handle_error(self, request, client_address):
        # Never dump tracebacks containing request data or upstream URLs.
        pass


class DashboardHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "KasaDashboard"
    sys_version = ""

    def setup(self):
        super().setup()
        self.connection.settimeout(20)

    def log_message(self, format, *args):
        pass

    def __getattr__(self, name):
        if name.startswith("do_"):
            return self._dispatch
        raise AttributeError(name)

    def handle_expect_100(self):
        # Do not emit an interim response before the request gate.
        return True

    def _security_headers(self):
        port = self.server.server_port
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        self.send_header("Content-Security-Policy", (
            "default-src 'self'; connect-src 'self' ws://127.0.0.1:%s ws://localhost:%s; "
            "img-src 'self' blob:; media-src 'self' blob:; style-src 'self' 'unsafe-inline'; "
            "frame-ancestors 'none'; base-uri 'none'; form-action 'none'") % (port, port))

    def _send(self, status, body=b"", content_type="text/plain; charset=utf-8", close=False):
        if isinstance(body, str):
            body = body.encode("utf-8")
        if not isinstance(content_type, str) or any(ord(char) < 32 or ord(char) == 127 for char in content_type):
            content_type = "application/octet-stream"
        self.send_response(status)
        self._security_headers()
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        if close or status >= 400:
            self.close_connection = True
            self.send_header("Connection", "close")
        self.end_headers()
        if self.command != "HEAD" and status != 204:
            self.wfile.write(body)

    def send_error(self, code, message=None, explain=None):
        self._send(code, "Request rejected.\n", close=True)

    def _json(self, status, body):
        self._send(status, json.dumps(body, ensure_ascii=True).encode("utf-8"), "application/json; charset=utf-8")

    def _body(self):
        if self._body_length > 4096:
            raise StoreError("too_large", "JSON body must be at most 4 KB.", 413)
        try:
            raw = self.rfile.read(self._body_length)
            if len(raw) != self._body_length:
                raise ValueError("Incomplete body")
            body = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeError, OSError, RecursionError):
            raise StoreError("invalid_json", "Send a valid JSON object.") from None
        if not isinstance(body, dict):
            raise StoreError("invalid_json", "Send a JSON object.")
        return body

    def _dispatch(self):
        # BaseHTTPRequestHandler normalizes a leading //; the gate needs the original target.
        self.path = self.requestline.split()[1]
        if self.request_version == "HTTP/0.9":
            self.request_version = "HTTP/1.0"
        headers = {key.lower(): value for key, value in self.headers.items()}
        # Repeated security-sensitive headers are ambiguous and cannot pass the gate.
        for key in ("host", "origin", "content-type", "sec-fetch-site"):
            if len(self.headers.get_all(key, [])) > 1:
                headers["host"] = ""
        status = check_request(self.command, self.path, headers, self.server.server_port)
        if status != 200:
            self._send(status, "Request rejected.\n", close=True)
            return
        lengths = self.headers.get_all("content-length", [])
        if "transfer-encoding" in headers or len(lengths) > 1 or (
                lengths and not re.fullmatch(r"[0-9]{1,10}", lengths[0])):
            self._send(400, "Invalid request framing.\n", close=True)
            return
        self._body_length = int(lengths[0]) if lengths else 0
        if self.command not in ("POST", "PATCH", "PUT") and self._body_length:
            self._send(400, "Unexpected request body.\n", close=True)
            return
        if "expect" in headers:
            self._send(417, "Expect is not supported.\n", close=True)
            return
        try:
            self._route(urlsplit(self.path).path, headers)
        except WeatherError as error:
            status = {"place_not_found": 404, "weather_unavailable": 502}.get(error.code, 400)
            self._json(status, {"error": error.code, "message": error.message})
        except StoreError as error:
            self._json(error.status, {"error": error.code, "message": error.message})
        except (OSError, http.client.HTTPException, RuntimeError, ValueError, TypeError):
            self._json(502, {"error": "go2rtc", "message": "The local stream service is unavailable."})

    def _route(self, path, headers):
        application = self.server.application
        if path == "/api/cameras":
            if self.command == "GET":
                self._json(200, application.metadata())
            else:
                self._json(201, application.add(self._body()))
        elif path == "/api/layout":
            self._json(200, application.set_layout(self._body()))
        elif path == "/api/weather":
            if self.command == "GET":
                result = application.weather.snapshot()
            else:
                result = application.weather.configure(self._body())
            self._json(200, result)
        elif path.startswith("/api/cameras/"):
            camera_id = path.rsplit("/", 1)[1]
            if self.command == "PATCH":
                self._json(200, application.rename(camera_id, self._body()))
            else:
                application.remove(camera_id)
                self._send(204)
        elif path in ("/api/frame.jpeg", "/api/ws"):
            camera_id = urlsplit(self.path).query[4:]
            application.camera(camera_id)
            if path == "/api/ws":
                self._websocket(camera_id, headers)
            else:
                status, content_type, body = application.bridge.frame(camera_id)
                if len(body) > MAX_FRAME:
                    self._send(502, "Snapshot exceeds the size limit.\n")
                else:
                    self._send(status, body, content_type)
        else:
            self._static(path)

    def _static(self, path):
        relative = "index.html" if path == "/" else unquote(path).lstrip("/")
        resolved = os.path.realpath(os.path.join(self.server.www, relative))
        if os.path.commonpath([self.server.www, resolved]) != self.server.www:
            self._send(404, "File not found.\n")
            return
        content_type = CONTENT_TYPES.get(Path(resolved).suffix.lower())
        if not content_type or not os.path.isfile(resolved):
            self._send(404, "File not found.\n")
            return
        self._send(200, Path(resolved).read_bytes(), content_type)

    def _buffered_client_bytes(self):
        self.connection.setblocking(False)
        try:
            buffered = self.rfile.peek(1)
            return self.rfile.read(len(buffered)) if buffered else b""
        except (BlockingIOError, OSError):
            return b""
        finally:
            self.connection.settimeout(15)

    def _websocket(self, camera_id, headers):
        try:
            key = base64.b64decode(headers.get("sec-websocket-key", ""), validate=True)
        except ValueError:
            key = b""
        if len(key) != 16 or headers.get("sec-websocket-version") != "13":
            self._send(400, "Invalid WebSocket handshake.\n")
            return
        if not self.server.relay_slots.acquire(blocking=False):
            self._send(503, "Too many video connections.\n")
            return
        upstream = None
        upgraded = False
        try:
            upstream = self.server.application.bridge.open_socket()
            request = ["GET /api/ws?src=%s HTTP/1.1" % camera_id, "Host: localhost",
                       "Origin: http://localhost", "Upgrade: websocket", "Connection: Upgrade"]
            for field in ("sec-websocket-key", "sec-websocket-version", "sec-websocket-protocol", "sec-websocket-extensions"):
                if field in headers:
                    value = headers[field]
                    if len(value) > 4096 or any(ord(char) < 32 or ord(char) == 127 for char in value):
                        self._send(400, "Invalid WebSocket header.\n")
                        return
                    request.append(field + ": " + value)
            upstream.sendall(("\r\n".join(request) + "\r\n\r\n").encode("latin-1"))
            response = bytearray()
            while b"\r\n\r\n" not in response:
                chunk = upstream.recv(4096)
                if not chunk or len(response) + len(chunk) > 65536:
                    self._send(503, "Stream upgrade unavailable.\n")
                    return
                response.extend(chunk)
            raw_headers, extra = bytes(response).split(b"\r\n\r\n", 1)
            status_line, header_block = raw_headers.split(b"\r\n", 1)
            if not re.fullmatch(rb"HTTP/1\.[01] 101(?: .*)?", status_line):
                self._send(503, "Stream upgrade unavailable.\n")
                return
            parsed = http.client.parse_headers(io.BytesIO(header_block + b"\r\n\r\n"))
            self.send_response(101)
            self._security_headers()
            for field in ("Upgrade", "Connection", "Sec-WebSocket-Accept", "Sec-WebSocket-Protocol", "Sec-WebSocket-Extensions"):
                value = parsed.get(field)
                if value and not any(ord(char) < 32 or ord(char) == 127 for char in value):
                    self.send_header(field, value)
            self.end_headers()
            self.wfile.flush()
            upgraded = True
            self.close_connection = True
            if self.connection.family == socket.AF_INET:
                self.connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            self.server.track_relay(self.connection, upstream)
            buffered = self._buffered_client_bytes()
            if buffered:
                upstream.sendall(buffered)
            if extra:
                self.wfile.write(extra)
            upstream.settimeout(15)
            self._pump(upstream)
        except (OSError, ValueError, http.client.HTTPException):
            if not upgraded:
                self._send(503, "Stream bridge is restarting.\n")
        finally:
            self.server.untrack_relay(self.connection, upstream)
            if upstream is not None:
                upstream.close()
            if upgraded:
                try:
                    self.connection.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
            self.server.relay_slots.release()

    def _pump(self, upstream):
        while True:
            ready, _, _ = select.select([self.connection, upstream], [], [], 1)
            for source in ready:
                data = source.recv(65536)
                if not data:
                    return
                destination = upstream if source is self.connection else self.connection
                destination.sendall(data)
