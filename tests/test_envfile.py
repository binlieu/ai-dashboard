import tempfile
from pathlib import Path
import unittest

from app.envfile import b64, env_get, json_string, split_csv, urlencode


class EnvfileTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "fixture"
        self.path.write_bytes(b'DOUBLE="double quoted value"\nSINGLE=\'single quoted value\'\n'
                              b'LITERAL=pa$word#part=value `literal`\nFIRST=first\nFIRST=second\n'
                              b'EMPTY=\nPADDED=  keep both spaces  \nUNMATCHED="keep this quote\n'
                              b'NESTED="\'inside\'"\nSHELL=$(printf unsafe)\nCRLF="windows value"\r\n'
                              b'FINAL=no newline')

    def test_dotenv_values_are_literal(self):
        for key, expected in {"DOUBLE": "double quoted value", "SINGLE": "single quoted value",
                              "LITERAL": 'pa$word#part=value `literal`', "FIRST": "first", "EMPTY": "",
                              "PADDED": "  keep both spaces  ", "UNMATCHED": '"keep this quote',
                              "NESTED": "'inside'", "SHELL": '$(printf unsafe)',
                              "CRLF": "windows value", "FINAL": "no newline"}.items():
            with self.subTest(key=key):
                self.assertEqual(env_get(self.path, key), expected)

    def test_missing_key_and_file(self):
        self.assertIsNone(env_get(self.path, "MISSING"))
        self.assertIsNone(env_get(self.path, "FIR"))
        self.assertIsNone(env_get(self.path.with_name("absent"), "KEY"))

    def test_only_one_cr_and_quote_pair_removed(self):
        self.path.write_bytes(b'KEY=""\r\nCR=x\r\r\n')
        self.assertEqual(env_get(self.path, "KEY"), "")
        self.assertEqual(env_get(self.path, "CR"), "x\r")

    def test_encoding(self):
        for value, expected in [("a.b+c@example.com", "a.b%2Bc%40example.com"),
                                 ('/+= $`#"\'', "%2F%2B%3D%20%24%60%23%22%27"),
                                 ("AZaz09-._~", "AZaz09-._~"), ("", "")]:
            with self.subTest(value=value):
                self.assertEqual(urlencode(value), expected)
        self.assertEqual(b64("secret1"), "c2VjcmV0MQ==")
        self.assertEqual(b64("a\n"), "YQo=")
        self.assertEqual(b64(""), "")
        self.assertEqual(b64("aaa" * 40), "YWFh" * 40)
        self.assertEqual(urlencode(b64("secret1")), "c2VjcmV0MQ%3D%3D")

    def test_csv(self):
        for value, expected in [(" a, , b ", ["a", "", "b"]), (",a,", ["", "a", ""]),
                                 ("", [""]), (",,", ["", "", ""]),
                                 ("\tFront door\t, Back yard ", ["Front door", "Back yard"]),
                                 ('a$`#=,b\\c', ['a$`#=', 'b\\c'])]:
            with self.subTest(value=value):
                self.assertEqual(split_csv(value), expected)

    def test_json_string(self):
        for value, expected in [('say "hi" \\ path', '"say \\"hi\\" \\\\ path"'),
                                 ('<script>&</script>', '"\\u003cscript\\u003e\\u0026\\u003c/script\\u003e"'),
                                 ('Cửa trước 日本', '"Cửa trước 日本"'),
                                 ('a\n\t\r\b\f\x01', '"a\\n\\t\\r\\b\\f\\u0001"'), ("", '""')]:
            with self.subTest(value=value):
                self.assertEqual(json_string(value), expected)
