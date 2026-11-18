"""Small AES-128-CBC implementation for local device messages (FIPS 197)."""


def _multiply(left, right):
    """Multiply bytes in GF(2**8), reduced by the AES polynomial."""
    result = 0
    for _ in range(8):
        if right & 1:
            result ^= left
        left = ((left << 1) ^ (0x11b if left & 0x80 else 0)) & 0xff
        right >>= 1
    return result


def _make_sbox():
    result = []
    for value in range(256):
        # The multiplicative inverse is value**254; zero maps to zero.
        inverse, base, exponent = 1, value, 254
        while exponent:
            if exponent & 1:
                inverse = _multiply(inverse, base)
            base = _multiply(base, base)
            exponent >>= 1
        substituted = inverse ^ 0x63
        for shift in range(1, 5):
            substituted ^= ((inverse << shift) | (inverse >> (8 - shift))) & 0xff
        result.append(substituted)
    return tuple(result)


_SBOX = _make_sbox()
_INVERSE_SBOX = tuple(_SBOX.index(value) for value in range(256))


def _expand_key(key):
    expanded = list(key)
    round_constant = 1
    while len(expanded) < 176:
        word = expanded[-4:]
        if len(expanded) % 16 == 0:
            word = [_SBOX[value] for value in word[1:] + word[:1]]
            word[0] ^= round_constant
            round_constant = _multiply(round_constant, 2)
        for value in word:
            expanded.append(expanded[-16] ^ value)
    return [expanded[offset:offset + 16] for offset in range(0, 176, 16)]


def _add_round_key(state, key):
    return [value ^ key[index] for index, value in enumerate(state)]


def _shift_rows(state, inverse=False):
    # AES stores each four-byte column consecutively.
    result = state[:]
    for row in range(4):
        for column in range(4):
            source = (column - row if inverse else column + row) % 4
            result[4 * column + row] = state[4 * source + row]
    return result


def _mix_columns(state, inverse=False):
    coefficients = (14, 11, 13, 9) if inverse else (2, 3, 1, 1)
    result = []
    for offset in range(0, 16, 4):
        column = state[offset:offset + 4]
        for row in range(4):
            value = 0
            for index in range(4):
                value ^= _multiply(column[index], coefficients[(index - row) % 4])
            result.append(value)
    return result


def _encrypt_block(block, round_keys):
    state = _add_round_key(list(block), round_keys[0])
    for round_number in range(1, 11):
        state = _shift_rows([_SBOX[value] for value in state])
        if round_number != 10:
            state = _mix_columns(state)
        state = _add_round_key(state, round_keys[round_number])
    return bytes(state)


def _decrypt_block(block, round_keys):
    state = _add_round_key(list(block), round_keys[10])
    for round_number in range(9, -1, -1):
        state = [_INVERSE_SBOX[value] for value in _shift_rows(state, inverse=True)]
        state = _add_round_key(state, round_keys[round_number])
        if round_number != 0:
            state = _mix_columns(state, inverse=True)
    return bytes(state)


def _validate(key, iv, data):
    if (not isinstance(key, bytes) or len(key) != 16 or
            not isinstance(iv, bytes) or len(iv) != 16 or not isinstance(data, bytes)):
        raise ValueError("Invalid AES input.")


def cbc_encrypt(key, iv, data):
    """Encrypt bytes with a 16-byte key/IV, always adding PKCS#7 padding."""
    _validate(key, iv, data)
    padding = 16 - len(data) % 16
    padded = data + bytes([padding]) * padding
    round_keys = _expand_key(key)
    result, previous = bytearray(), iv
    for offset in range(0, len(padded), 16):
        block = bytes(left ^ right for left, right in zip(padded[offset:offset + 16], previous))
        previous = _encrypt_block(block, round_keys)
        result.extend(previous)
    return bytes(result)


def cbc_decrypt(key, iv, data):
    """Decrypt bytes, rejecting incomplete ciphertext and invalid PKCS#7 padding."""
    _validate(key, iv, data)
    if not data or len(data) % 16:
        raise ValueError("Invalid AES ciphertext.")
    round_keys = _expand_key(key)
    result, previous = bytearray(), iv
    for offset in range(0, len(data), 16):
        block = data[offset:offset + 16]
        plain = _decrypt_block(block, round_keys)
        result.extend(left ^ right for left, right in zip(plain, previous))
        previous = block
    padding = result[-1]
    if not 1 <= padding <= 16 or result[-padding:] != bytes([padding]) * padding:
        raise ValueError("Invalid AES padding.")
    return bytes(result[:-padding])
