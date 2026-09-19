"""DCS (digital coded squelch) sub-audible signalling: 23-bit Golay word at 134.4 bit/s.

Word layout (verified against the published D023 example): 12 data bits =
9 code bits (the octal code) + marker '100' in bits 11..9, then 11 Golay
(23,12) check bits (generator 0xC75). The word goes on air LSB first, i.e.
the code bits come first, "backwards" compared to the octal notation.
Inverted DCS ("I") is the same word with the waveform polarity flipped.
"""
import numpy as np

BIT_RATE = 134.4
WORD_BITS = 23
# 48000 / 134.4 is not an integer (357.14 samples per bit). 7 words = 161 bits
# fit exactly into 57 500 samples at 48 kHz (bit = n * 7 // 2500), so the
# looped pattern has no seam.
WORDS_PER_LOOP = 7
LOOP_SAMPLES_48K = 57_500
LOWPASS_HZ = 200.0
LOWPASS_ORDER = 4

_GOLAY_POLY = 0xC75

STANDARD_CODES = tuple(int(c, 8) for c in (
    "023 025 026 031 032 043 047 051 054 065 071 072 073 074 114 115 116 125 131 132 134 143 152 "
    "155 156 162 165 172 174 205 223 226 243 244 245 251 261 263 265 271 306 311 315 331 343 346 "
    "351 364 365 371 411 412 413 423 431 432 445 464 465 466 503 506 516 532 546 565 606 612 624 "
    "627 631 632 654 662 664 703 712 723 731 732 734 743 754").split())

POLARITIES = ("N", "I")


def code_label(code: int) -> str:
    return f"{code:03o}"


def parse_code(text: str):
    """'023', '023N', 'D023I' -> (code, polarity). Raises ValueError for non-standard codes."""
    t = text.strip().upper().lstrip("D")
    polarity = "N"
    if t and t[-1] in POLARITIES:
        polarity = t[-1]
        t = t[:-1]
    code = int(t, 8)
    if code not in STANDARD_CODES:
        raise ValueError(f"{text!r} is not one of the 83 standard DCS codes")
    return code, polarity


def golay_word(code: int) -> int:
    """23-bit DCS word as an int (bit 0 is transmitted first)."""
    cw = 0o4000 | (code & 0x1FF)
    data = cw
    for _ in range(12):
        if cw & 1:
            cw ^= _GOLAY_POLY
        cw >>= 1
    return (cw << 12) | data


def word_bits(code: int):
    w = golay_word(code)
    return [(w >> i) & 1 for i in range(WORD_BITS)]


def render_loop(code: int, polarity: str = "N") -> np.ndarray:
    """One seamless 57 500-sample (48 kHz) loop of the filtered NRZ stream, peak +-1."""
    bits = np.array(word_bits(code) * WORDS_PER_LOOP, dtype=np.float32) * 2.0 - 1.0
    n = np.arange(LOOP_SAMPLES_48K)
    nrz = bits[(n * 7) // 2500]
    freqs = np.fft.rfftfreq(LOOP_SAMPLES_48K, 1.0 / 48000.0)
    response = 1.0 / np.sqrt(1.0 + (freqs / LOWPASS_HZ) ** (2 * LOWPASS_ORDER))
    out = np.fft.irfft(np.fft.rfft(nrz) * response, LOOP_SAMPLES_48K)
    out /= np.max(np.abs(out))
    if polarity == "I":
        out = -out
    return out.astype(np.float32)
