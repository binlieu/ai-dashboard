"""Acceptance tests for the device-control routes in the request gate (orchestrator-written;
do not modify). Device control switches real appliances, so every write must be same-origin."""

import unittest

from app.security import check_request

PORT = 1984
ORIGIN = "http://127.0.0.1:1984"
JSON = "application/json"


def h(**extra):
    headers = {"host": "127.0.0.1:1984"}
    headers.update({key.replace("_", "-"): value for key, value in extra.items()})
    return headers


class DeviceRoutes(unittest.TestCase):
    def test_list_can_be_read(self):
        self.assertEqual(check_request("GET", "/api/devices", h(), PORT), 200)
        self.assertEqual(check_request("GET", "/api/devices", h(sec_fetch_site="same-origin"), PORT), 200)

    def test_reads_rejected_from_other_sites(self):
        self.assertEqual(check_request("GET", "/api/devices", h(origin="http://evil.example"), PORT), 403)
        self.assertEqual(check_request("GET", "/api/devices", h(sec_fetch_site="cross-site"), PORT), 403)
        self.assertEqual(check_request("GET", "/api/devices", {"host": "evil.example:1984"}, PORT), 403)

    def test_same_origin_json_writes_allowed(self):
        ok = h(origin=ORIGIN, content_type=JSON)
        self.assertEqual(check_request("POST", "/api/devices", ok, PORT), 200)
        self.assertEqual(check_request("PATCH", "/api/devices/dev1", ok, PORT), 200)
        self.assertEqual(check_request("PUT", "/api/devices/dev12/state", ok, PORT), 200)
        self.assertEqual(check_request("DELETE", "/api/devices/dev1", h(origin=ORIGIN), PORT), 200)

    def test_writes_need_the_page_origin(self):
        for method, target in (("POST", "/api/devices"), ("PATCH", "/api/devices/dev1"),
                               ("PUT", "/api/devices/dev1/state"), ("DELETE", "/api/devices/dev1")):
            for headers in (h(content_type=JSON), h(origin="http://evil.example", content_type=JSON),
                            h(origin="null", content_type=JSON),
                            h(origin="http://127.0.0.1:1985", content_type=JSON),
                            h(origin=ORIGIN, content_type=JSON, sec_fetch_site="cross-site")):
                self.assertEqual(check_request(method, target, headers, PORT), 403, (method, headers))

    def test_writes_need_json(self):
        for content_type in (None, "text/plain", "application/x-www-form-urlencoded",
                             "multipart/form-data"):
            headers = h(origin=ORIGIN)
            if content_type:
                headers["content-type"] = content_type
            self.assertEqual(check_request("PUT", "/api/devices/dev1/state", headers, PORT), 415)
            self.assertEqual(check_request("POST", "/api/devices", headers, PORT), 415)

    def test_wrong_methods(self):
        ok = h(origin=ORIGIN, content_type=JSON)
        for method, target in (("PUT", "/api/devices"), ("DELETE", "/api/devices"),
                               ("GET", "/api/devices/dev1"), ("POST", "/api/devices/dev1"),
                               ("GET", "/api/devices/dev1/state"), ("POST", "/api/devices/dev1/state"),
                               ("DELETE", "/api/devices/dev1/state"), ("PATCH", "/api/devices/dev1/state")):
            self.assertEqual(check_request(method, target, ok, PORT), 405, (method, target))

    def test_only_well_formed_device_ids(self):
        ok = h(origin=ORIGIN, content_type=JSON)
        for target in ("/api/devices/", "/api/devices/dev0", "/api/devices/dev01", "/api/devices/cam1",
                       "/api/devices/dev12345", "/api/devices/dev1/", "/api/devices/dev1/state/",
                       "/api/devices/dev1/other", "/api/devices/dev1/state/x", "/api/devices/DEV1",
                       "/api/devices/dev1?on=1", "/api/devices/dev1/state?on=1", "/api/devices?x=1",
                       "/api/device", "/api/devices/dev1%2Fstate", "/api/devices/1.2.3.4"):
            for method in ("GET", "PUT", "PATCH", "DELETE", "POST"):
                self.assertIn(check_request(method, target, ok, PORT), (403, 404), (method, target))

    def test_camera_routes_are_unchanged(self):
        ok = h(origin=ORIGIN, content_type=JSON)
        self.assertEqual(check_request("PATCH", "/api/cameras/cam1", ok, PORT), 200)
        self.assertIn(check_request("PUT", "/api/cameras/cam1/state", ok, PORT), (403, 404))
        self.assertIn(check_request("PATCH", "/api/cameras/dev1", ok, PORT), (403, 404))


if __name__ == "__main__":
    unittest.main()
