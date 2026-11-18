"""Independent FIPS-197 and SP 800-38A vectors, plus padding failures."""

import unittest

from app.aes import (_SBOX, _INVERSE_SBOX, _decrypt_block, _encrypt_block, _expand_key,
                     cbc_decrypt, cbc_encrypt)


class AesTests(unittest.TestCase):
    def test_fips_197_sbox_table(self):
        # FIPS 197, section 5.1.1, Figure 7.
        expected = bytes.fromhex(
            "637c777bf26b6fc53001672bfed7ab76ca82c97dfa5947f0add4a2af9ca472c0"
            "b7fd9326363ff7cc34a5e5f171d8311504c723c31896059a071280e2eb27b275"
            "09832c1a1b6e5aa0523bd6b329e32f8453d100ed20fcb15b6acbbe394a4c58cf"
            "d0efaafb434d338545f9027f503c9fa851a3408f929d38f5bcb6da2110fff3d2"
            "cd0c13ec5f974417c4a77e3d645d197360814fdc222a908846eeb814de5e0bdb"
            "e0323a0a4906245cc2d3ac629195e479e7c8376d8dd54ea96c56f4ea657aae08"
            "ba78252e1ca6b4c6e8dd741f4bbd8b8a703eb5664803f60e613557b986c11d9e"
            "e1f8981169d98e949b1e87e9ce5528df8ca1890dbfe6426841992d0fb054bb16")
        self.assertEqual(bytes(_SBOX), expected)
        self.assertEqual([_INVERSE_SBOX[value] for value in _SBOX], list(range(256)))

    def test_fips_197_appendix_c1_block_and_key_schedule(self):
        # https://nvlpubs.nist.gov/nistpubs/FIPS/NIST.FIPS.197.pdf
        key = bytes.fromhex("000102030405060708090a0b0c0d0e0f")
        plain = bytes.fromhex("00112233445566778899aabbccddeeff")
        cipher = bytes.fromhex("69c4e0d86a7b0430d8cdb78070b4c55a")
        round_keys = _expand_key(key)
        self.assertEqual(bytes(round_keys[1]).hex(), "d6aa74fdd2af72fadaa678f1d6ab76fe")
        self.assertEqual(bytes(round_keys[10]).hex(), "13111d7fe3944a17f307a78b4d2b30c5")
        self.assertEqual(_encrypt_block(plain, round_keys), cipher)
        self.assertEqual(_decrypt_block(cipher, round_keys), plain)
        self.assertEqual(cbc_encrypt(key, bytes(16), plain)[:16], cipher)

    def test_nist_four_block_cbc_vector(self):
        key = bytes.fromhex("2b7e151628aed2a6abf7158809cf4f3c")
        iv = bytes(range(16))
        plain = bytes.fromhex(
            "6bc1bee22e409f96e93d7e117393172aae2d8a571e03ac9c9eb76fac45af8e51"
            "30c81c46a35ce411e5fbc1191a0a52eff69f2445df4f9b17ad2b417be66c3710")
        expected = bytes.fromhex(
            "7649abac8119b246cee98e9b12e9197d5086cb9b507219ee95db113a917678b2"
            "73bed6b8e3c1743b7116e69e222295163ff1caa1681fac09120eca307586e1a7")
        encrypted = cbc_encrypt(key, iv, plain)
        self.assertEqual(encrypted[:64], expected)
        self.assertEqual(len(encrypted), 80)
        self.assertEqual(cbc_decrypt(key, iv, encrypted), plain)

    def test_empty_binary_and_device_sized_messages(self):
        for size in (0, 1, 15, 16, 17, 31, 32, 255, 2048):
            with self.subTest(size=size):
                plain = bytes(index % 256 for index in range(size))
                encrypted = cbc_encrypt(bytes(16), bytes(reversed(range(16))), plain)
                self.assertIsInstance(encrypted, bytes)
                self.assertEqual(cbc_decrypt(bytes(16), bytes(reversed(range(16))), encrypted), plain)

    def test_invalid_types_and_lengths_do_not_echo_inputs(self):
        valid = bytes(16)
        for function in (cbc_encrypt, cbc_decrypt):
            for key, iv, data in ((b"PRIVATE", valid, valid), (valid, b"PRIVATE", valid),
                                  ("PRIVATE", valid, valid), (valid, None, valid),
                                  (valid, valid, "PRIVATE"), (valid, valid, bytearray(valid)),
                                  (valid, valid, None), (bytes(24), valid, valid)):
                with self.subTest(function=function.__name__, key_type=type(key), iv_type=type(iv)):
                    with self.assertRaises(ValueError) as caught:
                        function(key, iv, data)
                    self.assertNotIn("PRIVATE", str(caught.exception))
        for data in (b"", bytes(1), bytes(15), bytes(17)):
            with self.assertRaises(ValueError):
                cbc_decrypt(valid, valid, data)

    def test_rejects_invalid_padding_values_and_partial_padding(self):
        for invalid in (b"x" * 15 + b"\x00", b"x" * 15 + b"\x11",
                        b"x" * 14 + b"\x01\x02", b"\x0f" * 15 + b"\x10"):
            # The first encrypted block is a chosen plaintext block without valid padding.
            cipher = cbc_encrypt(bytes(16), bytes(16), invalid)[:16]
            with self.assertRaises(ValueError):
                cbc_decrypt(bytes(16), bytes(16), cipher)


if __name__ == "__main__":
    unittest.main()
