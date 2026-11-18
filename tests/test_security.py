"""Acceptance tests for the request gate. Written by the orchestrator — do NOT modify.

Run from the repo root:  /usr/bin/python3 -m unittest tests.test_security -v

Contract under test (app/security.py):

    check_request(method, target, headers, port) -> int

    method   HTTP method, upper-case string
    target   request target exactly as received (path plus optional ?query)
    headers  dict of request headers with lower-case names
    port     the TCP port the dashboard listens on (int)

    Returns 200 when the request may proceed, otherwise the HTTP status to
    reject it with (403, 404, 405 or 415). It never raises on bad input.
"""

import unittest

from app.security import check_request

PORT = 1984
HOST = "127.0.0.1:1984"
ORIGIN = "http://127.0.0.1:1984"
JSON = "application/json"


def h(**extra):
    headers = {"host": HOST}
    for key, value in extra.items():
        headers[key.replace("_", "-")] = value
    return headers


def ws(**extra):
    base = {
        "connection": "Upgrade",
        "upgrade": "websocket",
        "sec-websocket-version": "13",
        "sec-websocket-key": "dGhlIHNhbXBsZSBub25jZQ==",
        "origin": ORIGIN,
    }
    headers = h()
    headers.update(base)
    for key, value in extra.items():
        headers[key.replace("_", "-")] = value
    return headers


class HostHeader(unittest.TestCase):
    def test_loopback_ip_host_allowed(self):
        self.assertEqual(check_request("GET", "/", h(), PORT), 200)

    def test_localhost_host_allowed(self):
        self.assertEqual(check_request("GET", "/", {"host": "localhost:1984"}, PORT), 200)

    def test_missing_host_rejected(self):
        self.assertEqual(check_request("GET", "/", {}, PORT), 403)

    def test_dns_rebinding_host_rejected(self):
        self.assertEqual(check_request("GET", "/", {"host": "evil.example:1984"}, PORT), 403)

    def test_wrong_port_rejected(self):
        self.assertEqual(check_request("GET", "/", {"host": "127.0.0.1:8080"}, PORT), 403)

    def test_host_without_port_rejected(self):
        self.assertEqual(check_request("GET", "/", {"host": "127.0.0.1"}, PORT), 403)

    def test_lan_ip_host_rejected(self):
        self.assertEqual(check_request("GET", "/", {"host": "192.168.1.65:1984"}, PORT), 403)

    def test_host_check_applies_to_api(self):
        self.assertEqual(
            check_request("GET", "/api/cameras", {"host": "evil.example:1984"}, PORT), 403)


class StaticFiles(unittest.TestCase):
    def test_static_paths_allowed(self):
        for target in ("/", "/index.html", "/dashboard.js", "/dashboard.css",
                       "/vendor/video-rtc.js", "/vendor/video-stream.js"):
            self.assertEqual(check_request("GET", target, h(), PORT), 200, target)

    def test_head_allowed_for_static(self):
        self.assertEqual(check_request("HEAD", "/", h(), PORT), 200)

    def test_traversal_rejected(self):
        for target in ("/../.env", "/..%2f.env", "/%2e%2e/.env", "/vendor/../../.env",
                       "/.env", "/.git/config", "//etc/passwd", "/a\\..\\b"):
            self.assertIn(check_request("GET", target, h(), PORT), (403, 404), target)

    def test_post_to_static_rejected(self):
        self.assertIn(
            check_request("POST", "/", h(origin=ORIGIN, content_type=JSON), PORT), (404, 405))


class Go2rtcSurfaceIsClosed(unittest.TestCase):
    """Nothing of go2rtc's own API may be reachable except ws and frame.jpeg."""

    def test_dangerous_go2rtc_paths_rejected_for_every_method(self):
        targets = ("/api/config", "/api/exit", "/api/exit?code=0", "/api/streams",
                   "/api/streams?src=cam1", "/api/restart", "/api", "/api/",
                   "/api/stream.mp4?src=cam1", "/api/webrtc?src=cam1", "/api/log",
                   "/api/ffmpeg", "/api/stream.m3u8?src=cam1")
        for target in targets:
            for method in ("GET", "POST", "PUT", "PATCH", "DELETE"):
                status = check_request(
                    method, target, h(origin=ORIGIN, content_type=JSON), PORT)
                self.assertIn(status, (403, 404, 405), "%s %s" % (method, target))

    def test_frame_allowed_for_camera_ids_only(self):
        self.assertEqual(check_request("GET", "/api/frame.jpeg?src=cam1", h(), PORT), 200)
        self.assertEqual(check_request("GET", "/api/frame.jpeg?src=cam16", h(), PORT), 200)
        for target in ("/api/frame.jpeg", "/api/frame.jpeg?src=",
                       "/api/frame.jpeg?src=kasa://u:p@1.2.3.4",
                       "/api/frame.jpeg?src=exec:touch%20x",
                       "/api/frame.jpeg?src=cam1&src=exec:x",
                       "/api/frame.jpeg?src=cam1&name=x",
                       "/api/frame.jpeg?src=cam1%26x", "/api/frame.jpeg?src=camera",
                       "/api/frame.jpeg?src=cam-1", "/api/frame.jpeg?src=cam1/../x"):
            self.assertIn(check_request("GET", target, h(), PORT), (403, 404), target)

    def test_frame_rejected_from_other_sites(self):
        for site in ("cross-site", "same-site"):
            self.assertEqual(
                check_request("GET", "/api/frame.jpeg?src=cam1",
                              h(sec_fetch_site=site), PORT), 403, site)
        self.assertEqual(
            check_request("GET", "/api/frame.jpeg?src=cam1",
                          h(origin="https://evil.example"), PORT), 403)

    def test_frame_allowed_same_origin_fetch(self):
        self.assertEqual(
            check_request("GET", "/api/frame.jpeg?src=cam1",
                          h(sec_fetch_site="same-origin"), PORT), 200)


class WebSocket(unittest.TestCase):
    def test_same_origin_upgrade_allowed(self):
        self.assertEqual(check_request("GET", "/api/ws?src=cam2", ws(), PORT), 200)

    def test_localhost_origin_matches_localhost_host(self):
        self.assertEqual(
            check_request("GET", "/api/ws?src=cam2",
                          ws(host="localhost:1984", origin="http://localhost:1984"), PORT), 200)

    def test_missing_origin_rejected(self):
        headers = ws()
        del headers["origin"]
        self.assertEqual(check_request("GET", "/api/ws?src=cam1", headers, PORT), 403)

    def test_foreign_origin_rejected(self):
        for origin in ("https://evil.example", "http://127.0.0.1:1985", "null",
                       "https://127.0.0.1:1984", "http://127.0.0.1:1984.evil.example",
                       "http://localhost:1984"):
            self.assertEqual(
                check_request("GET", "/api/ws?src=cam1", ws(origin=origin), PORT), 403, origin)

    def test_non_camera_source_rejected(self):
        for target in ("/api/ws", "/api/ws?src=exec:x", "/api/ws?src=kasa://u:p@1.2.3.4",
                       "/api/ws?src=cam1&src=exec:x", "/api/ws?src=cam1&extra=1"):
            self.assertIn(check_request("GET", target, ws(), PORT), (403, 404), target)

    def test_plain_get_without_upgrade_rejected(self):
        self.assertIn(
            check_request("GET", "/api/ws?src=cam1", h(origin=ORIGIN), PORT), (403, 404, 405))


class AdminApi(unittest.TestCase):
    def test_reads_allowed(self):
        self.assertEqual(check_request("GET", "/api/cameras", h(), PORT), 200)
        self.assertEqual(
            check_request("GET", "/api/cameras", h(sec_fetch_site="same-origin"), PORT), 200)

    def test_reads_rejected_from_other_sites(self):
        self.assertEqual(
            check_request("GET", "/api/cameras", h(sec_fetch_site="cross-site"), PORT), 403)
        self.assertEqual(
            check_request("GET", "/api/cameras", h(origin="https://evil.example"), PORT), 403)

    def test_valid_writes_allowed(self):
        ok = h(origin=ORIGIN, content_type=JSON)
        self.assertEqual(check_request("POST", "/api/cameras", ok, PORT), 200)
        self.assertEqual(check_request("PATCH", "/api/cameras/cam3", ok, PORT), 200)
        self.assertEqual(check_request("DELETE", "/api/cameras/cam3", h(origin=ORIGIN), PORT), 200)
        self.assertEqual(check_request("PUT", "/api/layout", ok, PORT), 200)

    def test_json_content_type_with_charset_allowed(self):
        self.assertEqual(
            check_request("POST", "/api/cameras",
                          h(origin=ORIGIN, content_type="application/json; charset=utf-8"),
                          PORT), 200)

    def test_writes_without_origin_rejected(self):
        for method, target in (("POST", "/api/cameras"), ("PATCH", "/api/cameras/cam1"),
                               ("DELETE", "/api/cameras/cam1"), ("PUT", "/api/layout")):
            self.assertEqual(
                check_request(method, target, h(content_type=JSON), PORT), 403,
                "%s %s" % (method, target))

    def test_writes_with_foreign_origin_rejected(self):
        for origin in ("https://evil.example", "null", "http://127.0.0.1:1985",
                       "http://localhost:1984"):
            self.assertEqual(
                check_request("POST", "/api/cameras",
                              h(origin=origin, content_type=JSON), PORT), 403, origin)

    def test_simple_cross_site_form_post_rejected(self):
        # The exact shape that rewrote go2rtc's config: no preflight, text/plain body.
        for content_type in ("text/plain", "application/x-www-form-urlencoded",
                             "multipart/form-data; boundary=x", ""):
            status = check_request(
                "POST", "/api/cameras", h(origin=ORIGIN, content_type=content_type), PORT)
            self.assertEqual(status, 415, repr(content_type))

    def test_missing_content_type_on_body_write_rejected(self):
        self.assertEqual(check_request("POST", "/api/cameras", h(origin=ORIGIN), PORT), 415)
        self.assertEqual(check_request("PUT", "/api/layout", h(origin=ORIGIN), PORT), 415)

    def test_bad_camera_ids_rejected(self):
        ok = h(origin=ORIGIN, content_type=JSON)
        for target in ("/api/cameras/", "/api/cameras/1", "/api/cameras/cam",
                       "/api/cameras/cam1/extra", "/api/cameras/cam1%2f..",
                       "/api/cameras/../config", "/api/cameras/cam0x1"):
            self.assertIn(check_request("DELETE", target, ok, PORT), (403, 404), target)

    def test_wrong_methods_rejected(self):
        ok = h(origin=ORIGIN, content_type=JSON)
        self.assertIn(check_request("DELETE", "/api/cameras", ok, PORT), (404, 405))
        self.assertIn(check_request("POST", "/api/cameras/cam1", ok, PORT), (404, 405))
        self.assertIn(check_request("POST", "/api/layout", ok, PORT), (404, 405))
        self.assertIn(check_request("TRACE", "/", h(), PORT), (403, 404, 405))
        self.assertIn(check_request("CONNECT", "/", h(), PORT), (403, 404, 405))


class Robustness(unittest.TestCase):
    def test_never_raises_on_garbage(self):
        for target in ("", "*", "http://evil.example/", "/api/ws?src=%zz", "/\x00",
                       "/api/cameras?" + "a" * 5000, "/%"):
            status = check_request("GET", target, h(), PORT)
            self.assertIsInstance(status, int)
            self.assertNotEqual(status, 200, repr(target))

    def test_unknown_api_paths_rejected(self):
        self.assertIn(check_request("GET", "/api/anything", h(), PORT), (403, 404))


if __name__ == "__main__":
    unittest.main()
