"""Acceptance tests for local Kasa plug/switch control (orchestrator-written; do not modify).

The expected values were produced by a prototype that was checked against real HS103 and HS200
devices and against the published AES test vectors. No test here touches the network.
"""

import json
import unittest

from app.aes import cbc_decrypt, cbc_encrypt
from app.kasa_control import (ControlError, KlapSession, auth_hash, client_hash, legacy_frame,
                              legacy_request, legacy_unframe, parse_state, server_hash,
                              smart_request)

KEY = bytes.fromhex("2b7e151628aed2a6abf7158809cf4f3c")
IV = bytes(range(16))
LOCAL, REMOTE = bytes(range(16)), bytes(range(16, 32))
AUTH = bytes.fromhex("b117f522b0c3250db995447c37fe563ca79a0cd8eb892c80242eb746d7e3f7f5")
REQUEST = b'{"method":"get_device_info","params":null}'


class Aes(unittest.TestCase):
    def test_published_cbc_vector_then_padding_block(self):
        plain = bytes.fromhex("6bc1bee22e409f96e93d7e117393172a")
        self.assertEqual(cbc_encrypt(KEY, IV, plain).hex(),
                         "7649abac8119b246cee98e9b12e9197d8964e0b149c10b7b682e6e39aaeb731c")

    def test_short_and_multi_block_messages(self):
        self.assertEqual(cbc_encrypt(KEY, IV, b"kasa").hex(), "8cb0073c01b756f160248baaed1c9790")
        self.assertEqual(cbc_encrypt(KEY, IV, REQUEST).hex(),
                         "ca367ac4b54e52bd86bd363462a6a3af0a7f50acf5c06d8b145db889722f2983"
                         "0d832f46d23513594140b9b97a5c7e2e")

    def test_round_trip_for_every_length(self):
        for length in range(0, 70):
            plain = bytes((index * 7 + length) % 256 for index in range(length))
            cipher = cbc_encrypt(KEY, IV, plain)
            self.assertEqual(len(cipher), (length // 16 + 1) * 16)
            self.assertEqual(cbc_decrypt(KEY, IV, cipher), plain)

    def test_bad_input_is_a_value_error(self):
        good = cbc_encrypt(KEY, IV, b"kasa")
        for key, iv, data in ((KEY[:15], IV, good), (KEY, IV[:8], good), (KEY, IV, good[:15]),
                              (KEY, IV, b""), (KEY, IV, good[:-1] + bytes([good[-1] ^ 1]))):
            with self.assertRaises(ValueError):
                cbc_decrypt(key, iv, data)
        with self.assertRaises(ValueError):
            cbc_encrypt(KEY[:8], IV, b"x")


class Handshake(unittest.TestCase):
    def test_account_hash(self):
        self.assertEqual(auth_hash("user@example.com", "correct horse"), AUTH)

    def test_seed_hashes(self):
        self.assertEqual(server_hash(LOCAL, REMOTE, AUTH).hex(),
                         "321f4f894582f402dab62bd377e633c2165beee750fe104558108505cf03ff95")
        self.assertEqual(client_hash(LOCAL, REMOTE, AUTH).hex(),
                         "16f72baca667900c460f179616b82d1b8df02dba55f06bc98b969cbced604352")


class Session(unittest.TestCase):
    def test_sequence_and_signed_bodies(self):
        session = KlapSession(LOCAL, REMOTE, AUTH)
        seq, body = session.encrypt(REQUEST)
        self.assertEqual(seq, 1714446392)
        self.assertEqual(body.hex(),
                         "65acec5cf8075a91548c75f82b2396c0bf067fc8c2fd77a9bb5ea214407d8b30"
                         "07294b9d37c0a24ecfd0e8c3e1d99dd008bf24745ea37aa14d2edf877376087a"
                         "9f4517279b7f37212e8beacf9395816f")
        seq, body = session.encrypt(REQUEST)
        self.assertEqual(seq, 1714446393)
        self.assertEqual(body.hex(),
                         "328d832d602fa7f1134d870123e0fb418e16251ac08dd83f7fe5d9c6b9bea42a"
                         "15c797cbfe5ea1b56f6eb39217316dc8f798bf9ad0830aeb02153bb7cf9d93a7"
                         "f8a357c3df3c1838542ea5f78b906c20")

    def test_reply_is_decrypted_with_the_request_sequence(self):
        session = KlapSession(LOCAL, REMOTE, AUTH)
        reply = bytes.fromhex(
            "0000000000000000000000000000000000000000000000000000000000000000"
            "154666bd61b66c26dd015988f04f214c57e7c4a1646071ab70c25350c9f8f9d0"
            "a6b71cf177ef460beab8868d1470828e")
        self.assertEqual(json.loads(session.decrypt(1714446393, reply)),
                         {"result": {"device_on": True}, "error_code": 0})
        for bad in (reply[:40], b"", reply[:-1]):
            with self.assertRaises(ControlError):
                session.decrypt(1714446393, bad)


class Messages(unittest.TestCase):
    def test_legacy_frame(self):
        frame = legacy_frame({"system": {"get_sysinfo": {}}})
        self.assertEqual(frame.hex(),
                         "0000001dd0f281f88bff9af7d5ef94b6d1b4c09fec95e68fe187e8caf08bf68bf6")
        self.assertEqual(legacy_unframe(frame), {"system": {"get_sysinfo": {}}})
        for bad in (b"", frame[:3], frame[:10], b"\x00\x00\x00\x02ab", frame + b"x",
                    b"\x00\x10\x00\x00" + bytes(8)):
            with self.assertRaises(ControlError):
                legacy_unframe(bad)

    def test_requests(self):
        self.assertEqual(legacy_request(None), {"system": {"get_sysinfo": {}}})
        self.assertEqual(legacy_request(True), {"system": {"set_relay_state": {"state": 1}}})
        self.assertEqual(legacy_request(False), {"system": {"set_relay_state": {"state": 0}}})
        self.assertEqual(smart_request(None), {"method": "get_device_info", "params": None})
        self.assertEqual(smart_request(True),
                         {"method": "set_device_info", "params": {"device_on": True}})
        self.assertEqual(smart_request(False),
                         {"method": "set_device_info", "params": {"device_on": False}})

    def test_state_from_replies(self):
        sysinfo = lambda state: {"system": {"get_sysinfo": {"relay_state": state, "err_code": 0,
                                                            "alias": "PRIVATE"}}}
        self.assertIs(parse_state("legacy", sysinfo(1)), True)
        self.assertIs(parse_state("legacy", sysinfo(0)), False)
        smart = lambda on: {"result": {"device_on": on, "nickname": "PRIVATE"}, "error_code": 0}
        self.assertIs(parse_state("smart", smart(True)), True)
        self.assertIs(parse_state("smart", smart(False)), False)

    def test_unusable_replies_raise_control_error(self):
        bad = [("legacy", None), ("legacy", {}), ("legacy", {"system": {"get_sysinfo": {}}}),
               ("legacy", {"system": {"get_sysinfo": {"relay_state": 2, "err_code": 0}}}),
               ("legacy", {"system": {"get_sysinfo": {"relay_state": True, "err_code": 0}}}),
               ("legacy", {"system": {"get_sysinfo": {"relay_state": 1, "err_code": -1}}}),
               ("legacy", {"system": {"get_sysinfo": {"children": [{}], "err_code": 0}}}),
               ("smart", None), ("smart", {"error_code": 0}), ("smart", {"result": {}, "error_code": 0}),
               ("smart", {"result": {"device_on": 1}, "error_code": 0}),
               ("smart", {"result": {"device_on": True}, "error_code": -1501}),
               ("smart", {"result": {"device_on": True}}), ("other", {"result": {"device_on": True}})]
        for mode, reply in bad:
            with self.assertRaises(ControlError, msg=repr((mode, reply))):
                parse_state(mode, reply)

    def test_control_error_never_carries_device_text(self):
        try:
            parse_state("smart", {"result": {"nickname": "PRIVATE"}, "error_code": 0})
        except ControlError as error:
            self.assertNotIn("PRIVATE", str(error))


if __name__ == "__main__":
    unittest.main()
