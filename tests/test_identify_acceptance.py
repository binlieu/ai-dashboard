"""Acceptance tests for recognising non-camera Kasa devices (orchestrator-written; do not modify).

When a MAC address added in Setup is not a camera, the server asks the device what it is over
the two local Kasa discovery protocols and names it in the error message. Only a device kind
from a fixed list and a cleaned model name ever leave the parser.
"""

import binascii
import json
import socket
import unittest

from app.identify import (LEGACY_PORT, LEGACY_QUERY, NEW_PORT, NEW_QUERY, describe,
                          identify_device, parse_identity)


def legacy(payload):
    key, out = 171, bytearray()
    for byte in json.dumps(payload).encode():
        key ^= byte
        out.append(key)
    return bytes(out)


def new(result, error_code=0):
    return bytes(16) + json.dumps({"result": result, "error_code": error_code}).encode()


def sysinfo(**fields):
    base = {"sw_ver": "1.0.0", "alias": "PRIVATE-ALIAS", "deviceId": "PRIVATE-ID",
            "latitude_i": 123456, "longitude_i": -654321, "mac": "AA:BB:CC:DD:EE:01", "err_code": 0}
    base.update(fields)
    return {"system": {"get_sysinfo": base}}


class Queries(unittest.TestCase):
    def test_ports_and_exact_query_bytes(self):
        self.assertEqual((LEGACY_PORT, NEW_PORT), (9999, 20002))
        key, plain = 171, bytearray()
        for byte in LEGACY_QUERY:
            plain.append(key ^ byte)
            key = byte
        self.assertEqual(json.loads(bytes(plain)), {"system": {"get_sysinfo": {}}})
        self.assertEqual(NEW_QUERY, binascii.unhexlify("020000010000000000000000463cb5d3"))


class ParseIdentity(unittest.TestCase):
    def test_legacy_plug_and_switch(self):
        plug = legacy(sysinfo(model="HS103(US)", mic_type="IOT.SMARTPLUGSWITCH"))
        self.assertEqual(parse_identity(9999, plug), {"kind": "plug", "model": "HS103"})
        switch = legacy(sysinfo(model="HS200(US)", type="IOT.SMARTPLUGSWITCH"))
        self.assertEqual(parse_identity(9999, switch), {"kind": "switch", "model": "HS200"})

    def test_legacy_camera_nests_its_details_one_level_deeper(self):
        nested = sysinfo(model="KC410S(US)", type="IOT.IPCAMERA")["system"]["get_sysinfo"]
        camera = legacy({"system": {"get_sysinfo": {"system": nested, "err_code": 0}}})
        self.assertEqual(parse_identity(9999, camera), {"kind": "camera", "model": "KC410S"})

    def test_new_protocol(self):
        cases = [("HS200(US)", "SMART.KASASWITCH", "switch", "HS200"),
                 ("HS103(US)", "IOT.SMARTPLUGSWITCH", "plug", "HS103"),
                 ("KP125M(US)", "SMART.KASAPLUG", "plug", "KP125M"),
                 ("KS225(US)", "SMART.KASASWITCH", "switch", "KS225"),
                 ("KL130(US)", "IOT.SMARTBULB", "light", "KL130"),
                 ("L530E(EU)", "SMART.TAPOBULB", "light", "L530E"),
                 ("P110(UK)", "SMART.TAPOPLUG", "plug", "P110"),
                 ("X55", "HOMEWIFISYSTEM", "router", "X55"),
                 ("KC401(US)", "IOT.IPCAMERA", "camera", "KC401"),
                 ("C210", "SMART.IPCAMERA", "camera", "C210"),
                 ("KH100(US)", "SMART.KASAHUB", "other", "KH100")]
        for model, device_type, kind, clean in cases:
            data = new({"device_model": model, "device_type": device_type, "device_id": "PRIVATE-ID",
                        "owner": "PRIVATE-OWNER", "mac": "AA-BB-CC-DD-EE-01", "ip": "192.0.2.10"})
            self.assertEqual(parse_identity(20002, data), {"kind": kind, "model": clean}, model)

    def test_model_prefix_settles_the_shared_plug_switch_type(self):
        for model, kind in (("HS105(US)", "plug"), ("HS300(US)", "plug"), ("KP303(US)", "plug"),
                            ("EP10(US)", "plug"), ("HS210(US)", "switch"), ("HS220(US)", "switch"),
                            ("KS200M(US)", "switch"), ("ES20M(US)", "switch")):
            data = new({"device_model": model, "device_type": "IOT.SMARTPLUGSWITCH"})
            self.assertEqual(parse_identity(20002, data)["kind"], kind, model)
        unknown = new({"device_model": "ZZ999", "device_type": "IOT.SMARTPLUGSWITCH"})
        self.assertEqual(parse_identity(20002, unknown), {"kind": "plug_or_switch", "model": "ZZ999"})

    def test_type_without_a_usable_model(self):
        for model in (None, "", 5, "<script>", "HS\n200", "H" * 40, "(US)"):
            result = {"device_type": "SMART.KASASWITCH"}
            if model is not None:
                result["device_model"] = model
            self.assertEqual(parse_identity(20002, new(result)), {"kind": "switch", "model": None},
                             repr(model))

    def test_model_without_a_known_type(self):
        self.assertEqual(parse_identity(20002, new({"device_model": "HS200(US)"})),
                         {"kind": "switch", "model": "HS200"})
        self.assertEqual(parse_identity(20002, new({"device_model": "ZZ999", "device_type": 7})),
                         {"kind": "other", "model": "ZZ999"})

    def test_nothing_private_is_returned(self):
        data = legacy(sysinfo(model="HS103(US)", mic_type="IOT.SMARTPLUGSWITCH"))
        self.assertEqual(set(parse_identity(9999, data)), {"kind", "model"})

    def test_unusable_answers_give_none(self):
        camera_like = legacy({"system": {"get_sysinfo": {"err_code": -1, "system": "x"}}})
        bad = [(9999, b""), (9999, b"\x00" * 20), (9999, camera_like), (9999, legacy([])),
               (9999, legacy({"system": {"get_sysinfo": "x"}})), (9999, legacy(sysinfo())),
               (20002, b""), (20002, bytes(16)), (20002, bytes(16) + b"{"), (20002, new([])),
               (20002, new({})), (20002, new({"device_model": 5, "device_type": 6})),
               (20002, bytes(16) + b"[1]"), (20002, new({"device_type": "SMART.KASASWITCH"})[:10]),
               (1234, new({"device_model": "HS200", "device_type": "SMART.KASASWITCH"})),
               (9999, None), (20002, "text"),
               (20002, new({"device_model": "HS200", "device_type": "SMART.KASASWITCH",
                            "pad": "x" * 9000}))]
        for port, data in bad:
            self.assertIsNone(parse_identity(port, data), repr((port, data))[:80])


class Describe(unittest.TestCase):
    def test_messages(self):
        cases = [({"kind": "plug", "model": "HS103"},
                  "This is a Kasa smart plug (HS103), not a camera."),
                 ({"kind": "switch", "model": "HS200"},
                  "This is a Kasa smart switch (HS200), not a camera."),
                 ({"kind": "plug_or_switch", "model": "ZZ999"},
                  "This is a Kasa smart plug or switch (ZZ999), not a camera."),
                 ({"kind": "light", "model": "KL130"},
                  "This is a Kasa smart light (KL130), not a camera."),
                 ({"kind": "router", "model": "X55"},
                  "This is a TP-Link Wi-Fi router (X55), not a camera."),
                 ({"kind": "other", "model": "KH100"},
                  "This is a TP-Link device (KH100), not a camera."),
                 ({"kind": "switch", "model": None},
                  "This is a Kasa smart switch, not a camera."),
                 ({"kind": "camera", "model": "KC401"},
                  "This looks like a camera (KC401), but it did not accept a video connection. "
                  "Check that it is online and try again.")]
        for identity, message in cases:
            self.assertEqual(describe(identity), message)

    def test_unknown_device_keeps_the_old_message(self):
        self.assertEqual(describe(None), "The address did not answer as a Kasa camera.")


class FakeSocket:
    """A UDP socket whose replies are scripted; records everything sent."""

    def __init__(self, replies):
        self.replies, self.sent, self.closed, self.timeouts = list(replies), [], False, []

    def settimeout(self, value):
        self.timeouts.append(value)

    def sendto(self, data, address):
        self.sent.append((data, address))

    def recvfrom(self, size):
        if not self.replies:
            raise TimeoutError()
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply

    def close(self):
        self.closed = True


class IdentifyDevice(unittest.TestCase):
    SWITCH = new({"device_model": "HS200(US)", "device_type": "SMART.KASASWITCH"})

    def run_with(self, replies, ip="192.0.2.10"):
        self.socket = FakeSocket(replies)
        return identify_device(ip, timeout=0.2, socket_factory=lambda: self.socket)

    def test_asks_only_the_given_address_on_both_ports(self):
        self.assertEqual(self.run_with([(self.SWITCH, ("192.0.2.10", 20002))]),
                         {"kind": "switch", "model": "HS200"})
        self.assertTrue(self.socket.sent)
        self.assertEqual({address for _, address in self.socket.sent},
                         {("192.0.2.10", 9999), ("192.0.2.10", 20002)})
        for data, (_, port) in self.socket.sent:
            self.assertEqual(data, LEGACY_QUERY if port == 9999 else NEW_QUERY)
        self.assertTrue(self.socket.closed)

    def test_ignores_answers_from_other_hosts_and_ports(self):
        replies = [(self.SWITCH, ("192.0.2.99", 20002)), (self.SWITCH, ("192.0.2.10", 5353))]
        self.assertIsNone(self.run_with(replies))
        self.assertTrue(self.socket.closed)

    def test_skips_an_unusable_answer_and_uses_the_next(self):
        camera_like = legacy({"system": {"get_sysinfo": {"err_code": -1, "system": "x"}}})
        replies = [(camera_like, ("192.0.2.10", 9999)), (self.SWITCH, ("192.0.2.10", 20002))]
        self.assertEqual(self.run_with(replies), {"kind": "switch", "model": "HS200"})

    def test_a_quiet_first_wait_resends_and_keeps_listening(self):
        for quiet in (TimeoutError(), socket.timeout()):
            replies = [quiet, (self.SWITCH, ("192.0.2.10", 20002))]
            self.assertEqual(self.run_with(replies), {"kind": "switch", "model": "HS200"})
            self.assertGreaterEqual(len(self.socket.sent), 4)

    def test_silence_and_socket_errors_give_none(self):
        self.assertIsNone(self.run_with([]))
        self.assertIsNone(self.run_with([OSError("network down")]))
        self.assertTrue(self.socket.closed)

        def broken():
            raise OSError("no sockets")
        self.assertIsNone(identify_device("192.0.2.10", timeout=0.2, socket_factory=broken))

    def test_rejects_anything_that_is_not_an_ipv4_address(self):
        for ip in ("", "example.com", "255.255.255.255", "192.0.2.10:9999", None, "::1", "0.0.0.0",
                   "224.0.0.1"):
            factory_calls = []
            result = identify_device(ip, timeout=0.2,
                                     socket_factory=lambda: factory_calls.append(1) or FakeSocket([]))
            self.assertIsNone(result, repr(ip))
            self.assertEqual(factory_calls, [], repr(ip))


if __name__ == "__main__":
    unittest.main()
