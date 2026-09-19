"""POCSAG (ITU-R M.584) codewords, batches, text coding and a bit-level batch decoder.

Pure Python/numpy, no GNU Radio: shared by the TX encoder (pocsag.py) and the RX deframer
(pluto_advanced_rx/pocsag_deframer.py).

Format: preamble of alternating bits (>= 576), then batches of a sync codeword and 8 frames x 2
codewords. Every codeword is 32 bits, sent MSB first: BCH(31,21) (generator x^10+x^9+x^8+x^6+x^5+x^3+1)
plus an even parity bit. Address codeword: 0 + 18 address bits + 2 function bits; message codeword:
1 + 20 data bits. The frame in which an address is sent is RIC & 7. Logic 1 is the LOWER FM frequency.
"""
import time

SYNC = 0x7CD215D8
IDLE = 0x7A89C197
PREAMBLE_BITS = 576
BATCH_WORDS = 16
BCH_POLY = 0x769  # 11101101001b, x^10 + x^9 + x^8 + x^6 + x^5 + x^3 + 1
RIC_MAX = 0x1FFFFF
BAUD_RATES = (512, 1200, 2400)

NUMERIC_CHARS = "0123456789*U -]["  # index = 4-bit code, sent LSB first
_DE_TO_CHAR = {0x5B: "Ä", 0x5C: "Ö", 0x5D: "Ü", 0x7B: "ä", 0x7C: "ö", 0x7D: "ü", 0x7E: "ß"}  # DIN 66003
_CHAR_TO_DE = {v: k for k, v in _DE_TO_CHAR.items()}
CHARSETS = ("ascii", "de")


def _parity(x: int) -> int:
    return bin(x).count("1") & 1


def bch_remainder(v31: int) -> int:
    """Remainder of the 31-bit value (bits 30..0) divided by the BCH generator polynomial."""
    for i in range(30, 9, -1):
        if (v31 >> i) & 1:
            v31 ^= BCH_POLY << (i - 10)
    return v31 & 0x3FF


def make_codeword(data21: int) -> int:
    """21 data bits (flag + 20) -> 32-bit codeword with BCH check bits and even parity."""
    body = (data21 << 10) | bch_remainder(data21 << 10)
    return (body << 1) | _parity(body)


def address_codeword(address18: int, function: int) -> int:
    return make_codeword(((address18 & 0x3FFFF) << 2) | (function & 3))


def message_codeword(data20: int) -> int:
    return make_codeword((1 << 20) | (data20 & 0xFFFFF))


def is_valid(cw: int) -> bool:
    return bch_remainder(cw >> 1) == 0 and _parity(cw) == 0


def _build_syndromes():
    table = {}
    for i in range(31):                       # weight 1 first so it wins over weight-2 collisions
        table.setdefault(bch_remainder(1 << i), 1 << (i + 1))
    for i in range(31):
        for j in range(i + 1, 31):
            table.setdefault(bch_remainder((1 << i) | (1 << j)), (1 << (i + 1)) | (1 << (j + 1)))
    return table


_SYNDROMES = _build_syndromes()


def correct_codeword(cw: int):
    """-> (corrected_cw, n_bit_errors) for up to 2 bit errors, else None."""
    cw &= 0xFFFFFFFF
    s = bch_remainder(cw >> 1)
    flips = 0
    if s:
        flips = _SYNDROMES.get(s)
        if flips is None:
            return None
        cw ^= flips
    errors = bin(flips).count("1")
    if _parity(cw):                            # remaining parity mismatch: the parity bit itself was hit
        cw ^= 1
        errors += 1
    if errors > 2 or not is_valid(cw):
        return None
    return cw, errors


# ---------------------------------------------------------------- text coding

def _encode_char_de(ch: str) -> int:
    if ch in _CHAR_TO_DE:
        return _CHAR_TO_DE[ch]
    code = ord(ch)
    if code < 0x80 and chr(code) not in "[\\]{|}~":
        return code
    return ord("?")


def text_to_bits(text: str, kind: str = "alpha", charset: str = "ascii") -> list:
    """Message payload as a bit list padded to whole 20-bit codewords (alpha: one EOT then zeros;
    numeric: spaces)."""
    bits = []
    if kind == "numeric":
        for ch in text.upper():
            code = NUMERIC_CHARS.index(ch) if ch in NUMERIC_CHARS else 12
            bits += [(code >> i) & 1 for i in range(4)]
        while len(bits) % 20:
            bits += [(12 >> i) & 1 for i in range(4)]
        return bits
    for ch in text:
        code = _encode_char_de(ch) if charset == "de" else (ord(ch) if ord(ch) < 0x80 else ord("?"))
        bits += [(code >> i) & 1 for i in range(7)]
    bits += [(0x04 >> i) & 1 for i in range(7)]
    bits += [0] * (-len(bits) % 20)
    return bits


def bits_to_text(bits, kind: str = "alpha", charset: str = "ascii") -> str:
    if kind == "numeric":
        out = []
        for i in range(0, len(bits) - 3, 4):
            out.append(NUMERIC_CHARS[sum(bits[i + k] << k for k in range(4))])
        return "".join(out).rstrip()
    out = []
    for i in range(0, len(bits) - 6, 7):
        code = sum(bits[i + k] << k for k in range(7))
        if code == 0x04:
            break
        if charset == "de" and code in _DE_TO_CHAR:
            out.append(_DE_TO_CHAR[code])
        elif code in (0x0A, 0x0D) or 0x20 <= code < 0x7F:
            out.append(chr(code))
    return "".join(out).rstrip("\x00")


# ---------------------------------------------------------------- transmission

def _word_bits(cw: int):
    return [(cw >> (31 - i)) & 1 for i in range(32)]


def build_transmission(ric: int, function: int, payload_bits=None, preamble_bits: int = PREAMBLE_BITS):
    """Bit list of one paging call: preamble, then batches (sync + 16 codewords) carrying the address
    codeword in frame RIC & 7 followed by the payload codewords; unused slots are idle codewords."""
    if not 0 <= ric <= RIC_MAX:
        raise ValueError(f"RIC must be 0..{RIC_MAX}")
    if not 0 <= function <= 3:
        raise ValueError("function must be 0..3")
    address = address_codeword(ric >> 3, function)
    if address == 0:
        raise ValueError("RIC 0 with function 0 is the all-zero codeword (indistinguishable from silence)")
    words = [IDLE] * (2 * (ric & 7))
    words.append(address)
    payload_bits = list(payload_bits or [])
    if len(payload_bits) % 20:
        raise ValueError("payload must be whole 20-bit codewords")
    for i in range(0, len(payload_bits), 20):
        words.append(message_codeword(int("".join(map(str, payload_bits[i:i + 20])), 2)))
    words += [IDLE] * (-len(words) % BATCH_WORDS)
    bits = [1 - (i & 1) for i in range(preamble_bits)]
    for start in range(0, len(words), BATCH_WORDS):
        bits += _word_bits(SYNC)
        for cw in words[start:start + BATCH_WORDS]:
            bits += _word_bits(cw)
    return bits


# ---------------------------------------------------------------- decoder

class BatchDecoder:
    """Feed demodulated bits one by one; finished calls are handed to `on_message` as dicts
    (ric, function, payload = '0'/'1' string, corrected, uncorrectable, polarity, time).

    Hunts for the sync codeword in both polarities (<= 2 bit errors), then follows batches (a sync every
    17 words); after two missing syncs it hunts again."""

    def __init__(self, on_message=None, sync_errors: int = 2, max_missed_syncs: int = 2):
        self.on_message = on_message
        self.sync_errors = sync_errors
        self.max_missed = max_missed_syncs
        self.batches = 0
        self.codewords_ok = 0
        self.codewords_bad = 0
        self.locked = False
        self.last_lock_time = 0.0
        self._reset_hunt()

    def _reset_hunt(self):
        self._reg = 0
        self._state = "hunt"
        self._nbits = 0
        self._words = []
        self._missed = 0
        self._msg = None
        self.locked = False

    def push_bit(self, bit: int):
        self._reg = ((self._reg << 1) | (bit & 1)) & 0xFFFFFFFF
        if self._state == "hunt":
            for inv in (0, 1):
                ref = SYNC ^ (0xFFFFFFFF if inv else 0)
                if bin(self._reg ^ ref).count("1") <= self.sync_errors:
                    self._polarity = inv
                    self._begin_batch()
                    return
            return
        self._nbits += 1
        if self._nbits < 32:
            return
        self._nbits = 0
        word = self._reg ^ (0xFFFFFFFF if self._polarity else 0)
        if self._state == "sync":
            if bin(word ^ SYNC).count("1") <= self.sync_errors + 1:
                self._begin_batch()
            else:
                self._missed += 1
                self._finish_message()
                if self._missed >= self.max_missed:
                    self._reset_hunt()
                else:
                    self._begin_batch(missed=True)
            return
        self._codeword(word, len(self._words))
        self._words.append(word)
        if len(self._words) == BATCH_WORDS:
            self._state = "sync"
            self._words = []

    def _begin_batch(self, missed=False):
        if not missed:
            self._missed = 0
            self.batches += 1
            self.locked = True
            self.last_lock_time = time.time()
        self._state = "batch"
        self._nbits = 0
        self._words = []

    def _codeword(self, word, index):
        if word in (0, 0xFFFFFFFF):  # silence/noise floor decodes as a valid all-zero "address 0": not a call
            self._finish_message()
            return
        if bin(word ^ IDLE).count("1") <= 3:  # the one codeword every receiver knows in advance
            self.codewords_ok += 1
            self._finish_message()
            return
        fixed = correct_codeword(word)
        # A random 32-bit word lies within two bit errors of SOME codeword with ~26 % probability, so noise
        # would otherwise produce phantom calls: a new address is only trusted with at most one corrected bit.
        if fixed is not None and fixed[0] >> 31 == 0 and fixed[1] > 1:
            fixed = None
        if fixed is None:
            self.codewords_bad += 1
            if self._msg is not None:
                self._msg["uncorrectable"] += 1
                self._msg["bits"] += "0" * 20
            return
        cw, errors = fixed
        self.codewords_ok += 1
        if cw == IDLE:
            self._finish_message()
            return
        if cw >> 31 == 0:
            self._finish_message()
            self._msg = dict(ric=(((cw >> 13) & 0x3FFFF) << 3) | (index >> 1), function=(cw >> 11) & 3,
                             bits="", corrected=errors, uncorrectable=0, polarity=self._polarity)
        elif self._msg is not None:
            self._msg["bits"] += format((cw >> 11) & 0xFFFFF, "020b")
            self._msg["corrected"] += errors

    def _finish_message(self):
        msg, self._msg = self._msg, None
        if msg is not None and self.on_message is not None:
            msg["payload"] = msg.pop("bits")
            msg["time"] = time.time()
            self.on_message(msg)

    def flush(self):
        self._finish_message()


def decode_message(msg: dict, interpretation: str = "auto", charset: str = "ascii") -> str:
    """Payload of a decoded call as text. interpretation: auto (function 0 = numeric, otherwise alpha),
    alpha, numeric."""
    bits = [int(c) for c in msg.get("payload", "")]
    kind = interpretation
    if kind == "auto":
        kind = "numeric" if msg.get("function") == 0 else "alpha"
    return bits_to_text(bits, kind, charset)
