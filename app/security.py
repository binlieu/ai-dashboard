"""Pure request allowlist shared by every HTTP handler."""

import re
from urllib.parse import unquote, urlsplit

CAMERA_ID = re.compile(r"cam[1-9][0-9]{0,3}\Z")
DEVICE_ID = re.compile(r"dev[1-9][0-9]{0,3}\Z")


def check_request(method, target, headers, port):
    try:
        return _check_request(method, target, headers, port)
    except Exception:
        # Invalid input is always a rejection, never a handler exception.
        return 403


def _check_request(method, target, headers, port):
    host = headers.get("host")
    if host not in ("127.0.0.1:%s" % port, "localhost:%s" % port):
        return 403
    if not isinstance(target, str) or not target.startswith("/") or len(target) > 2048:
        return 403
    if any(ord(char) <= 32 or ord(char) == 127 for char in target):
        return 403
    if "\\" in target or "//" in target or "#" in target:
        return 403
    if re.search(r"%(?![0-9a-fA-F]{2})|%(?:00|2e|2f|5c|25)", target, re.I):
        return 403
    parsed = urlsplit(target)
    path = unquote(parsed.path, errors="strict")
    if parsed.scheme or parsed.netloc or any(part.startswith(".") for part in path.split("/")):
        return 403
    if any(ord(char) < 32 or ord(char) == 127 for char in path):
        return 403

    same_origin = headers.get("origin") == "http://" + host
    foreign = ("origin" in headers and not same_origin) or (
        "sec-fetch-site" in headers and headers["sec-fetch-site"] not in ("same-origin", "none"))
    if path == "/api" or path.startswith("/api/"):
        if foreign:
            return 403
        has_query = "?" in target
        if path in ("/api/ws", "/api/frame.jpeg"):
            if method != "GET":
                return 405
            if not re.fullmatch(r"src=cam[1-9][0-9]{0,3}", parsed.query):
                return 404
            if path == "/api/ws":
                if not same_origin:
                    return 403
                connection = [token.strip().lower() for token in headers.get("connection", "").split(",")]
                if headers.get("upgrade", "").lower() != "websocket" or "upgrade" not in connection:
                    return 403
            return 200
        if path == "/api/cameras":
            methods = ("GET", "POST")
        elif path == "/api/devices":
            methods = ("GET", "POST")
        elif path.startswith("/api/devices/"):
            device_path = path[len("/api/devices/"):]
            if DEVICE_ID.fullmatch(device_path):
                methods = ("PATCH", "DELETE")
            elif device_path.endswith("/state") and DEVICE_ID.fullmatch(device_path[:-6]):
                methods = ("PUT",)
            else:
                return 404
        elif path == "/api/layout":
            methods = ("PUT",)
        elif path == "/api/weather":
            methods = ("GET", "PUT")
        elif path.startswith("/api/cameras/") and CAMERA_ID.fullmatch(path[len("/api/cameras/"):]):
            methods = ("PATCH", "DELETE")
        else:
            return 404
        if has_query:
            return 404
        if method not in methods:
            return 405
        if method != "GET":
            if not same_origin:
                return 403
            if method in ("POST", "PATCH", "PUT"):
                media_type = headers.get("content-type", "").split(";", 1)[0].strip().lower()
                if media_type != "application/json":
                    return 415
        return 200
    return 200 if method in ("GET", "HEAD") else 405
