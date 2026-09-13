"""Pure-function BPSK31 text encoder: renders an ASCII/Latin-1 string into a
real audio waveform (a raised-cosine-shaped, differentially-encoded BPSK
signal at the fixed 31.25 baud PSK31 symbol rate, centered on an operator-
chosen audio tone) suitable for injection into this app's existing
Hilbert-based USB modulation chain -- same architectural role as
digitext.py's encode_text(), same signature shape (returns
(audio: np.ndarray[float32], duration_s: float)).

Deliberately Qt/GNU-Radio-free (same separation as digitext.py/
fft_probe.py) -- testable with synthetic text alone, no hardware or running
flowgraph needed.

## Varicode

PSK31's character encoding, developed by Peter Martinez (G3PLX) and later
adopted as an ITU-R recommendation. Variable-length (1-10 bits): common
characters get shorter codes (space=1 bit, 'e'=2 bits) to approximate
Huffman-optimal coding for English text at a fixed 31.25 baud. Every valid
code starts AND ends with a '1' bit and never contains two consecutive '0'
bits internally -- this is what makes it self-synchronizing: characters are
separated by two consecutive '0' bits ("00"), a pattern that provably
cannot occur inside a valid code, so no explicit framing/length field is
needed at all (confirmed for all 256 entries below via an automated check
before adopting this table -- no "00" substring, every entry starts/ends
with '1').

`_VARICODE_TABLE` below (index = ASCII/Latin-1 code point, value = the bit
pattern as a '0'/'1' string) is sourced from fldigi's own `pskvaricode.cxx`
-- the reference desktop implementation this project's PSK31 mode needs to
actually interoperate with -- via ckoval7/pydigi's `psk_varicode.py` (a
from-source Python reimplementation of fldigi's modems that carries the
same table forward), not hand-derived or guessed.

## BPSK modulation (differential encoding)

A phase reversal (180 deg) encodes a '0' bit; no phase change encodes a
'1' bit -- NRZI-style DIFFERENTIAL encoding of the Varicode bit stream, not
a direct bit->absolute-phase mapping. This is deliberate: it means the
receiver's Costas loop (which locks BPSK to within a 180-degree ambiguity,
by construction -- a real carrier-phase-recovery loop cannot itself tell
0 degrees from 180 degrees apart) never needs to resolve that ambiguity at
all, since only the RELATIVE phase transition from one symbol to the next
carries information, not the absolute phase of any one symbol.
`_differential_encode()` implements this directly: an internal "current
phase state" bit starts at an arbitrary reference (1) and flips exactly
when the input Varicode bit is 0, stays the same when the input bit is 1;
the OUTPUT is this running state sequence, one entry per input bit.

## Raised-cosine pulse shaping

Per the ARRL/G3PLX PSK31 spec: "the phase shifting occurs at an amplitude
minimum and a raised cosine filter makes sure the amplitude transition is
as bandwidth preserving as possible." Implemented here directly in the
time domain (not via a separate FIR filter stage): each symbol's
amplitude envelope is 1.0 (full amplitude) for the samples where neither
neighboring symbol differs in phase, and ramps down to/up from 0 via a
quarter-cosine taper over the first/second half of the symbol wherever a
phase change actually occurs on that side -- so amplitude only ever dips
to zero exactly where a real phase reversal happens (never at a same-
phase boundary), matching the spec's own description and the standard
from-scratch PSK31 synthesis technique (confirmed against a public
worked example, swharden.com's "Experiments in PSK-31 Synthesis").

Exact envelope/rolloff shape is a starting point, not a final tuned value
-- this project's own established discipline (see pluto_tx/filebroadcast.py's
Phase 0 history) is to verify real DSP parameters against real hardware
before trusting them; treat anything here as subject to revision once a
real over-the-air round trip is tested.
"""
import numpy as np

PSK31_SYMBOL_RATE_HZ = 31.25  # fixed by international PSK31 convention, not adjustable

_VARICODE_TABLE = [
    "1010101011",  # 0
    "1011011011",  # 1
    "1011101101",  # 2
    "1101110111",  # 3
    "1011101011",  # 4
    "1101011111",  # 5
    "1011101111",  # 6
    "1011111101",  # 7
    "1011111111",  # 8
    "11101111",  # 9
    "11101",  # 10
    "1101101111",  # 11
    "1011011101",  # 12
    "11111",  # 13
    "1101110101",  # 14
    "1110101011",  # 15
    "1011110111",  # 16
    "1011110101",  # 17
    "1110101101",  # 18
    "1110101111",  # 19
    "1101011011",  # 20
    "1101101011",  # 21
    "1101101101",  # 22
    "1101010111",  # 23
    "1101111011",  # 24
    "1101111101",  # 25
    "1110110111",  # 26
    "1101010101",  # 27
    "1101011101",  # 28
    "1110111011",  # 29
    "1011111011",  # 30
    "1101111111",  # 31
    "1",  # 32 ' '
    "111111111",  # 33 '!'
    "101011111",  # 34 '"'
    "111110101",  # 35 '#'
    "111011011",  # 36 '$'
    "1011010101",  # 37 '%'
    "1010111011",  # 38 '&'
    "101111111",  # 39 "'"
    "11111011",  # 40 '('
    "11110111",  # 41 ')'
    "101101111",  # 42 '*'
    "111011111",  # 43 '+'
    "1110101",  # 44 ','
    "110101",  # 45 '-'
    "1010111",  # 46 '.'
    "110101111",  # 47 '/'
    "10110111",  # 48 '0'
    "10111101",  # 49 '1'
    "11101101",  # 50 '2'
    "11111111",  # 51 '3'
    "101110111",  # 52 '4'
    "101011011",  # 53 '5'
    "101101011",  # 54 '6'
    "110101101",  # 55 '7'
    "110101011",  # 56 '8'
    "110110111",  # 57 '9'
    "11110101",  # 58 ':'
    "110111101",  # 59 ';'
    "111101101",  # 60 '<'
    "1010101",  # 61 '='
    "111010111",  # 62 '>'
    "1010101111",  # 63 '?'
    "1010111101",  # 64 '@'
    "1111101",  # 65 'A'
    "11101011",  # 66 'B'
    "10101101",  # 67 'C'
    "10110101",  # 68 'D'
    "1110111",  # 69 'E'
    "11011011",  # 70 'F'
    "11111101",  # 71 'G'
    "101010101",  # 72 'H'
    "1111111",  # 73 'I'
    "111111101",  # 74 'J'
    "101111101",  # 75 'K'
    "11010111",  # 76 'L'
    "10111011",  # 77 'M'
    "11011101",  # 78 'N'
    "10101011",  # 79 'O'
    "11010101",  # 80 'P'
    "111011101",  # 81 'Q'
    "10101111",  # 82 'R'
    "1101111",  # 83 'S'
    "1101101",  # 84 'T'
    "101010111",  # 85 'U'
    "110110101",  # 86 'V'
    "101011101",  # 87 'W'
    "101110101",  # 88 'X'
    "101111011",  # 89 'Y'
    "1010101101",  # 90 'Z'
    "111110111",  # 91 '['
    "111101111",  # 92 '\\'
    "111111011",  # 93 ']'
    "1010111111",  # 94 '^'
    "101101101",  # 95 '_'
    "1011011111",  # 96 '`'
    "1011",  # 97 'a'
    "1011111",  # 98 'b'
    "101111",  # 99 'c'
    "101101",  # 100 'd'
    "11",  # 101 'e'
    "111101",  # 102 'f'
    "1011011",  # 103 'g'
    "101011",  # 104 'h'
    "1101",  # 105 'i'
    "111101011",  # 106 'j'
    "10111111",  # 107 'k'
    "11011",  # 108 'l'
    "111011",  # 109 'm'
    "1111",  # 110 'n'
    "111",  # 111 'o'
    "111111",  # 112 'p'
    "110111111",  # 113 'q'
    "10101",  # 114 'r'
    "10111",  # 115 's'
    "101",  # 116 't'
    "110111",  # 117 'u'
    "1111011",  # 118 'v'
    "1101011",  # 119 'w'
    "11011111",  # 120 'x'
    "1011101",  # 121 'y'
    "111010101",  # 122 'z'
    "1010110111",  # 123 '{'
    "110111011",  # 124 '|'
    "1010110101",  # 125 '}'
    "1011010111",  # 126 '~'
    "1110110101",  # 127
    "1110111101",  # 128
    "1110111111",  # 129
    "1111010101",  # 130
    "1111010111",  # 131
    "1111011011",  # 132
    "1111011101",  # 133
    "1111011111",  # 134
    "1111101011",  # 135
    "1111101101",  # 136
    "1111101111",  # 137
    "1111110101",  # 138
    "1111110111",  # 139
    "1111111011",  # 140
    "1111111101",  # 141
    "1111111111",  # 142
    "10101010101",  # 143
    "10101010111",  # 144
    "10101011011",  # 145
    "10101011101",  # 146
    "10101011111",  # 147
    "10101101011",  # 148
    "10101101101",  # 149
    "10101101111",  # 150
    "10101110101",  # 151
    "10101110111",  # 152
    "10101111011",  # 153
    "10101111101",  # 154
    "10101111111",  # 155
    "10110101011",  # 156
    "10110101101",  # 157
    "10110101111",  # 158
    "10110110101",  # 159
    "10110110111",  # 160
    "10110111011",  # 161
    "10110111101",  # 162
    "10110111111",  # 163
    "10111010101",  # 164
    "10111010111",  # 165
    "10111011011",  # 166
    "10111011101",  # 167
    "10111011111",  # 168
    "10111101011",  # 169
    "10111101101",  # 170
    "10111101111",  # 171
    "10111110101",  # 172
    "10111110111",  # 173
    "10111111011",  # 174
    "10111111101",  # 175
    "10111111111",  # 176
    "11010101011",  # 177
    "11010101101",  # 178
    "11010101111",  # 179
    "11010110101",  # 180
    "11010110111",  # 181
    "11010111011",  # 182
    "11010111101",  # 183
    "11010111111",  # 184
    "11011010101",  # 185
    "11011010111",  # 186
    "11011011011",  # 187
    "11011011101",  # 188
    "11011011111",  # 189
    "11011101011",  # 190
    "11011101101",  # 191
    "11011101111",  # 192
    "11011110101",  # 193
    "11011110111",  # 194
    "11011111011",  # 195
    "11011111101",  # 196
    "11011111111",  # 197
    "11101010101",  # 198
    "11101010111",  # 199
    "11101011011",  # 200
    "11101011101",  # 201
    "11101011111",  # 202
    "11101101011",  # 203
    "11101101101",  # 204
    "11101101111",  # 205
    "11101110101",  # 206
    "11101110111",  # 207
    "11101111011",  # 208
    "11101111101",  # 209
    "11101111111",  # 210
    "11110101011",  # 211
    "11110101101",  # 212
    "11110101111",  # 213
    "11110110101",  # 214
    "11110110111",  # 215
    "11110111011",  # 216
    "11110111101",  # 217
    "11110111111",  # 218
    "11111010101",  # 219
    "11111010111",  # 220
    "11111011011",  # 221
    "11111011101",  # 222
    "11111011111",  # 223
    "11111101011",  # 224
    "11111101101",  # 225
    "11111101111",  # 226
    "11111110101",  # 227
    "11111110111",  # 228
    "11111111011",  # 229
    "11111111101",  # 230
    "11111111111",  # 231
    "101010101011",  # 232
    "101010101101",  # 233
    "101010101111",  # 234
    "101010110101",  # 235
    "101010110111",  # 236
    "101010111011",  # 237
    "101010111101",  # 238
    "101010111111",  # 239
    "101011010101",  # 240
    "101011010111",  # 241
    "101011011011",  # 242
    "101011011101",  # 243
    "101011011111",  # 244
    "101011101011",  # 245
    "101011101101",  # 246
    "101011101111",  # 247
    "101011110101",  # 248
    "101011110111",  # 249
    "101011111011",  # 250
    "101011111101",  # 251
    "101011111111",  # 252
    "101101010101",  # 253
    "101101010111",  # 254
    "101101011011",  # 255
]


def _text_to_bits(text):
    """Text -> a flat list of 0/1 ints: each character's Varicode code
    followed by the "00" inter-character separator. Characters outside
    Latin-1 (0-255) fall back to '?' (mirrors the reference implementation's
    own out-of-range handling) rather than raising -- a chat message is
    free-typed operator input, not a validated protocol field."""
    bits = []
    fallback = _VARICODE_TABLE[ord("?")]
    for ch in text:
        code = _VARICODE_TABLE[ord(ch)] if ord(ch) < 256 else fallback
        bits.extend(int(b) for b in code)
        bits.extend((0, 0))
    return bits


def _differential_encode(bits):
    """Varicode bits -> a same-length list of 0/1 "phase state" values (see
    module docstring's Differential encoding section). state starts at an
    arbitrary reference (1) and flips exactly when the current bit is 0."""
    state = 1
    states = []
    for b in bits:
        if b == 0:
            state ^= 1
        states.append(state)
    return states


def _synthesize_bpsk(states, sample_rate, tone_hz, amplitude):
    """Differentially-encoded phase states -> real audio samples: one
    BPSK31 symbol per state, raised-cosine-shaped so amplitude only dips
    to zero where a real phase reversal happens relative to a neighboring
    symbol (see module docstring's Raised-cosine pulse shaping section)."""
    sps = sample_rate / PSK31_SYMBOL_RATE_HZ
    n_sym = len(states)
    if n_sym == 0:
        return np.zeros(0, dtype=np.float32)
    signs = np.array([1.0 if s else -1.0 for s in states], dtype=np.float64)
    bounds = [int(round(n * sps)) for n in range(n_sym + 1)]
    out = np.zeros(bounds[-1], dtype=np.float64)
    for n in range(n_sym):
        n0, n1 = bounds[n], bounds[n + 1]
        length = n1 - n0
        if length <= 0:
            continue
        half = length // 2
        env = np.ones(length, dtype=np.float64)
        prev_differs = n > 0 and signs[n] != signs[n - 1]
        next_differs = n < n_sym - 1 and signs[n] != signs[n + 1]
        if prev_differs and half > 0:
            env[:half] = np.sin((np.arange(half) + 0.5) / half * (np.pi / 2))
        if next_differs and (length - half) > 0:
            rest = length - half
            env[half:] = np.cos((np.arange(rest) + 0.5) / rest * (np.pi / 2))
        t = (n0 + np.arange(length)) / sample_rate
        out[n0:n1] = signs[n] * env * np.cos(2 * np.pi * tone_hz * t)
    peak = np.max(np.abs(out))
    if peak > 0:
        out *= amplitude / peak
    return out.astype(np.float32)


def encode_text(text, sample_rate, tone_hz, tail_s=0.15, amplitude=0.7, preamble_chars=200):
    """Returns (audio: np.ndarray[float32], duration_s: float) -- same
    shape as digitext.encode_text(), suitable for feeding this app's
    existing Hilbert-based USB modulation chain (own dedicated instances,
    see pluto_tx/flowgraph.py's MODE_PSK31 branch).

    preamble_chars: a real, confirmed-necessary finding from this mode's
    own Phase 0 testing (mirrors GFSK's identical "real hardware/DSP loops
    need settle time" lesson from pluto_tx/filebroadcast.py's own history):
    a pure software round-trip test with NO preamble at all decoded the
    entire message correctly except its first character -- the receiver's
    Costas loop (carrier phase) and symbol_sync_ff (symbol timing) both
    need a short run-up before they've actually locked.

    Emitted as ordinary space characters (Varicode's own shortest code,
    "1", one bit) PREPENDED TO THE TEXT ITSELF -- i.e. run through the
    exact same single, continuous `_text_to_bits`/`_differential_encode`
    pass as the real message, not a separately-generated raw phase
    pattern glued on afterward. A real bug found and fixed during this
    same Phase 0 testing: an earlier version generated the preamble as
    raw ALTERNATING phase-reversal states, bypassing Varicode/differential
    encoding entirely, on the theory that a strongly periodic pattern
    locks the receive loops fastest (mirroring GFSK Stage 3's own
    alternating-preamble finding) -- but gluing a separately-generated
    state sequence onto a FRESH differential-encoder run for the real
    text creates a phase discontinuity exactly at the seam (the real
    encoder always starts from its own fixed reference state, with no
    knowledge of what phase the preamble ended on), corrupting the first
    character in a way that depended on the preamble's exact length
    (specifically its length's parity) in a fragile, hard-to-predict way
    -- confirmed by testing several preamble lengths, ALL of which
    corrupted the first character, just via different failure shapes.
    Prepending real idle CHARACTERS instead avoids the whole bug class:
    there is no seam at all, since the preamble and the real message
    share one uninterrupted differential-encoding run, with a completely
    ordinary "00" character boundary landing naturally between the last
    idle space and the first real character -- confirmed via a real
    end-to-end software round-trip test (encode -> real GNU Radio Costas
    loop/symbol_sync_ff/diff_decoder_bb chain -> psk31_deframer.py)
    correctly recovering 100% of a real message this way, including its
    first character, where the alternating-state approach did not.

    tail_s: true trailing silence appended after the signal, same
    rationale as digitext.encode_text()'s own tail_s (belt-and-suspenders
    alongside the GUI's auto-unkey timer -- guarantees the last thing the
    modulator ever sees for this transmission is genuine silence)."""
    if not text:
        text = " "
    full_text = (" " * max(0, preamble_chars)) + text
    bits = _text_to_bits(full_text)
    states = _differential_encode(bits)
    audio = _synthesize_bpsk(states, sample_rate, tone_hz, amplitude)
    if tail_s > 0:
        audio = np.concatenate([audio, np.zeros(int(tail_s * sample_rate), dtype=np.float32)])
    return audio, len(audio) / sample_rate


def estimate_duration(text, tail_s=0.15, preamble_chars=200):
    """Cheap preview estimate (bit-count only, no actual synthesis) for a
    live GUI readout while typing -- mirrors digitext.
    estimate_bandwidth_and_duration()'s same "lazy, expensive part happens
    only at send time" idea. Returns duration_s."""
    if not text:
        text = " "
    full_text = (" " * max(0, preamble_chars)) + text
    n_symbols = len(_text_to_bits(full_text))
    return n_symbols / PSK31_SYMBOL_RATE_HZ + tail_s
