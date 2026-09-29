"""JS8 physical layer: 12-character frame + 3 transmission flags -> 87 bits (CRC12) -> LDPC(174,87) ->
79 8-FSK tones, and back. Port of JS8Call's JS8::encode() / checkCRC12() / extractmessage174()
(JS8_Mode/JS8.cpp, v2.5.2); every constant comes from pluto_tx/js8_tables.py and is documented with its
source line in docs/js8/SPEC.md (section 1).

Layout of the 79 tones (SPEC 1.4): Costas A | 29 parity symbols | Costas B | 29 message symbols |
Costas C, 3 bits per tone MSB first, no Gray code. Codeword order as the decoder sees it:
[87 parity bits | 87 message bits]."""
import numpy as np

from . import js8_tables as T

NUM_SYMBOLS = T.NUM_SYMBOLS              # 79
N = T.LDPC["N"]                          # 174
K = T.LDPC["K"]                          # 87
FRAME_CHARS = 12
PARITY_POS = 7                           # first parity symbol
MESSAGE_POS = 43                         # first message symbol
COSTAS_POS = (0, 36, 72)

# Varicode::SubmodeType
NORMAL, FAST, TURBO, SLOW = 0, 1, 2, 4
SUBMODES = {sm: T.SUBMODES[sm] for sm in (NORMAL, FAST, TURBO, SLOW)}
SUBMODE_NAMES = {sm: d["name"].lower() for sm, d in SUBMODES.items()}

# Varicode::TransmissionType -- the 3 flag bits sent with every frame
FLAG_FIRST = T.TRANSMISSION_TYPES["JS8CallFirst"]
FLAG_LAST = T.TRANSMISSION_TYPES["JS8CallLast"]
FLAG_DATA = T.TRANSMISSION_TYPES["JS8CallData"]

_ALPHA_INDEX = {c: i for i, c in enumerate(T.PHY_ALPHABET)}


def _parity_matrix():
    rows = []
    for h in T.PARITY_HEX:
        bits = "".join(f"{int(c, 16):04b}" for c in h)[:K]
        rows.append([int(b) for b in bits])
    return np.array(rows, dtype=np.uint8)


PARITY = _parity_matrix()                # 87 x 87: parity bit i = PARITY[i] . message (mod 2)


def check_matrix():
    """H (87 x 174) of the codeword [parity | message], from the decoder's Tanner graph (LDPC_NM)."""
    h = np.zeros((N - K, N), dtype=np.uint8)
    for chk, bits in enumerate(T.LDPC_NM):
        h[chk, list(bits)] = 1
    return h


def submode_info(submode):
    """-> dict: name, symbol_samples, period_s, start_delay_ms, costas, rx thresholds, plus derived
    symbol_period_s, tone_spacing_hz, bandwidth_hz, data_duration_s (JS8Submode.cpp:57-68)."""
    d = dict(SUBMODES[submode])
    rate = T.RX_SAMPLE_RATE
    d["symbol_period_s"] = d["symbol_samples"] / rate
    d["tone_spacing_hz"] = rate / d["symbol_samples"]
    d["bandwidth_hz"] = 8 * rate / d["symbol_samples"]
    d["data_duration_s"] = NUM_SYMBOLS * d["symbol_samples"] / rate
    return d


def costas(submode):
    return T.COSTAS[SUBMODES[submode]["costas"]]


def crc12(data: bytes) -> int:
    """boost::augmented_crc<12, 0xC06>(data) ^ 42 (JS8.cpp:887): MSB-first shift register, initial 0,
    the message is expected to already carry the 12 zero bits the CRC goes into."""
    width, poly = T.CRC12["width"], T.CRC12["poly"]
    top, mask = 1 << (width - 1), (1 << width) - 1
    rem = 0
    for byte in data:
        for k in range(7, -1, -1):
            quotient = rem & top
            rem = ((rem << 1) | ((byte >> k) & 1)) & mask
            if quotient:
                rem ^= poly
    return rem ^ T.CRC12["xor_out"]


def frame_to_bits(frame: str, flags: int):
    """-> 87 message bits (numpy uint8): 72 payload bits (6 per character), 3 flag bits, CRC12."""
    if len(frame) != FRAME_CHARS:
        raise ValueError(f"a JS8 frame has {FRAME_CHARS} characters, got {len(frame)}: {frame!r}")
    try:
        words = [_ALPHA_INDEX[c] for c in frame]
    except KeyError as e:
        raise ValueError(f"invalid character {e.args[0]!r} in JS8 frame {frame!r}") from None
    value = 0
    for w in words:
        value = (value << 6) | w
    value = (value << 3) | (flags & 0b111)                 # 75 bits
    raw = (value << 13).to_bytes(11, "big")              # 88-bit array, CRC field still zero
    crc = crc12(raw)
    value = (value << 12) | crc                          # 87 bits
    return np.array([(value >> (K - 1 - i)) & 1 for i in range(K)], dtype=np.uint8)


def bits_to_frame(msg_bits):
    """87 message bits -> (frame, flags), or None if the CRC does not match (JS8.cpp:890-938)."""
    msg_bits = np.asarray(msg_bits, dtype=np.uint8)
    value = 0
    for b in msg_bits[:75]:
        value = (value << 1) | int(b)
    crc_rx = 0
    for b in msg_bits[75:87]:
        crc_rx = (crc_rx << 1) | int(b)
    if crc12((value << 13).to_bytes(11, "big")) != crc_rx:
        return None
    flags = value & 0b111
    payload = value >> 3
    frame = "".join(T.PHY_ALPHABET[(payload >> (6 * (11 - i))) & 63] for i in range(FRAME_CHARS))
    return frame, flags


def parity_bits(msg_bits):
    return (PARITY @ np.asarray(msg_bits, dtype=np.uint8)) % 2


def encode(frame: str, flags: int, submode: int):
    """-> 79 tone indices (numpy int8) for one frame, like JS8::encode(flags, Costas(submode), frame)."""
    msg = frame_to_bits(frame, flags)
    par = parity_bits(msg)
    tones = np.zeros(NUM_SYMBOLS, dtype=np.int8)
    for pos, arr in zip(COSTAS_POS, costas(submode)):
        tones[pos:pos + 7] = arr
    tones[PARITY_POS:PARITY_POS + 29] = _bits_to_tones(par)
    tones[MESSAGE_POS:MESSAGE_POS + 29] = _bits_to_tones(msg)
    return tones


def _bits_to_tones(bits):
    b = np.asarray(bits, dtype=np.int8).reshape(-1, 3)
    return 4 * b[:, 0] + 2 * b[:, 1] + b[:, 2]


def _tones_to_bits(tones):
    t = np.asarray(tones, dtype=np.int8)
    return np.stack([(t >> 2) & 1, (t >> 1) & 1, t & 1], axis=1).reshape(-1).astype(np.uint8)


def codeword_from_tones(tones):
    """79 tones -> 174-bit codeword [parity | message] (hard decision)."""
    t = np.asarray(tones)
    return np.concatenate([_tones_to_bits(t[PARITY_POS:PARITY_POS + 29]),
                           _tones_to_bits(t[MESSAGE_POS:MESSAGE_POS + 29])])


def decode_tones(tones):
    """Error-free tones -> (frame, flags), or None if parity or CRC do not check."""
    cw = codeword_from_tones(tones)
    if np.any(parity_bits(cw[K:]) != cw[:K]):
        return None
    return bits_to_frame(cw[K:])


def costas_ok(tones, submode):
    t = np.asarray(tones)
    return all(tuple(t[p:p + 7]) == tuple(a) for p, a in zip(COSTAS_POS, costas(submode)))
