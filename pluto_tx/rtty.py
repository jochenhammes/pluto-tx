"""Pure-function RTTY text encoder: renders an ASCII string into a real
audio waveform (continuous-phase 2-tone FSK at an operator-chosen baud
rate/mark frequency/shift) suitable for injection into this app's
existing Hilbert-based USB modulation chain -- same architectural role
and (audio: np.ndarray[float32], duration_s: float) return contract as
psk31.py's/digitext.py's own encode_text().

Deliberately Qt/GNU-Radio-free (same separation as psk31.py/digitext.py)
-- testable with synthetic text alone, no hardware or running flowgraph
needed.

## Baudot/ITA2 code (US/commercial FIGS variant)

RTTY's character encoding is NOT the same table as ITU's pure ITA2 --
ham RTTY traffic conventionally uses the US/commercial FIGS variant
(different punctuation assignments in the shifted "figures" case).
`_LTRS_TABLE`/`_FIGS_TABLE` below are transcribed verbatim from
dl-fldigi's `rtty.cxx` (a real, from-source fldigi derivative used for
actual RTTY traffic -- https://github.com/ukhas/dl-fldigi/blob/master/
src/cw_rtty/rtty.cxx), the same "cite a real reference implementation"
discipline psk31.py's Varicode table follows (fldigi's own
pskvaricode.cxx), not hand-derived or recalled from memory. Index 27
(0b11011) and 31 (0b11111) are reserved shift-control codes (FIGS/LTRS
respectively) -- both tables carry a placeholder ' ' there since a real
decoder always intercepts those codes as shift commands before ever
doing a character-table lookup; this module's own reverse-lookup tables
(`_LTRS_REVERSE`/`_FIGS_REVERSE`) explicitly exclude both indices so
they can never be mistaken for real space characters (real space is a
separate, valid entry at index 4 in both tables).

Bits are transmitted LSB-first within each 5-bit code (confirmed
against the same reference's `send_char()`), framed the standard
asynchronous-RTTY way: 1 start bit (space) + 5 data bits (LSB first) +
a `stop_bits`-length stop element (mark) -- see `_codes_to_frames()`.

## Mark/Space and Normal/Reverse

`mark_hz` is the operator-facing reference tone; `space_hz = mark_hz +
shift_hz` always (i.e. shift is a magnitude, not a sign). "Reverse"
swaps which of the two physical tones represents the logical mark/space
states (`_tone_for_bit()`) -- this is the standard real-world RTTY
"Normal/Reverse" toggle, used to correct for a counterpart station/rig
whose tone assignment is flipped relative to this one, not a change to
which frequency is numerically higher.

## Continuous-phase FSK synthesis

Unlike PSK31's per-symbol-independent raised-cosine segments,
`_synthesize_fsk()` carries a single running phase accumulator across
every bit boundary in ONE pass over the full frame list (preamble +
real message together) -- never resets, never re-synthesizes two
buffers separately and concatenates them. This matters for the exact
same reason documented in `psk31.py`'s own docstring (search
"phase discontinuity"): PSK31 originally broke its first real character
by gluing a separately-generated preamble state sequence onto a fresh
encoder pass; the FSK-specific version of that same mistake would be
synthesizing the preamble and the text as two separate
`_synthesize_fsk()` calls and `np.concatenate`-ing the audio, which
would reintroduce a phase discontinuity (and an audible/spectral click)
at the seam even though FSK itself has no absolute-phase ambiguity to
avoid. `encode_text()` therefore always builds ONE combined frame list
and makes exactly one `_synthesize_fsk()` call over it.

## Preamble

Continuous idle MARK for `preamble_s` seconds before the real message
-- exactly what a real, unkeyed-but-warming-up RTTY transmitter's idle
line state already looks like, so it costs nothing extra spectrally and
lets the receive chain's filters/AGC settle before real content starts.

Deliberately NOT the classic "RY diddle" (alternating mark/space)
tuning signal some RTTY gear historically sends: this project's RX
deframer (pluto_advanced_rx/rtty_deframer.py) uses open-loop,
edge-triggered UART-style framing rather than a continuously-tracking
clock-recovery loop (see that module's own docstring for why -- Baudot
framing is genuinely asynchronous, unlike PSK31's disguised-synchronous
idle). An alternating preamble would therefore generate a stream of
spurious mark->space edges, each triggering a wasted (and occasionally
falsely-valid-looking) frame-decode attempt before the real message
even starts -- confirmed by an actual encode/decode round-trip test
during development, which is what caught this. Continuous mark has
none of that: zero transitions, zero spurious frame triggers, and the
real first character's own start-bit edge is unambiguous.
"""
import numpy as np

# US/commercial FIGS variant, LSB-first per-bit transmission order.
# Index 27 = FIGS-shift code (0b11011), index 31 = LTRS-shift code
# (0b11111) -- both carry an unused ' ' placeholder, see module docstring.
_LTRS_TABLE = [
    "\0", "E", "\n", "A", " ", "S", "I", "U",
    "\r", "D", "R", "J", "N", "F", "C", "K",
    "T", "Z", "L", "W", "H", "Y", "P", "Q",
    "O", "B", "G", " ", "M", "X", "V", " ",
]
_FIGS_TABLE = [
    "\0", "3", "\n", "-", " ", "\a", "8", "7",
    "\r", "$", "4", "'", ",", "!", ":", "(",
    "5", '"', ")", "2", "#", "6", "0", "1",
    "9", "?", "&", " ", ".", "/", ";", " ",
]
FIGS_SHIFT = 0b11011  # 27
LTRS_SHIFT = 0b11111  # 31


def _build_reverse(table):
    rev = {}
    for code, ch in enumerate(table):
        if code in (FIGS_SHIFT, LTRS_SHIFT) or not ch or ch == "\0":
            continue
        rev.setdefault(ch, code)
    return rev


_LTRS_REVERSE = _build_reverse(_LTRS_TABLE)
_FIGS_REVERSE = _build_reverse(_FIGS_TABLE)


def _tone_for_bit(bit, mark_hz, space_hz, reverse):
    """bit=1 is the logical MARK state (idle/stop), bit=0 is SPACE
    (start/data-0) -- see module docstring's Normal/Reverse section."""
    if reverse:
        return space_hz if bit else mark_hz
    return mark_hz if bit else space_hz


def _text_to_baudot_codes(text):
    """Text -> a flat list of 5-bit ints, including inserted LTRS/FIGS
    shift codes wherever the current shift state must change. Starts in
    LTRS (the real-world convention). Characters common to both tables
    (space, CR, LF -- same code either way) never trigger a shift code
    of their own, but a SPACE always resets the tracked shift_state back
    to LTRS afterward (USOS, "Unshift On Space" -- a standard real-world
    RTTY convention, matching the RX deframer's own identical USOS
    behavior in pluto_advanced_rx/rtty_deframer.py; both sides must agree
    on this or shift state desyncs across a space -- confirmed by an
    actual encode/decode round-trip test during development, which is
    what caught the original mismatch). CR/LF deliberately do NOT trigger
    USOS -- real convention scopes it to space specifically. Letters are
    case-folded to upper (Baudot has no case). Unsupported characters
    fall back to FIGS '?' (mirrors psk31.py's own '?' fallback for
    out-of-range input -- free-typed operator text, not a validated
    protocol field)."""
    codes = []
    shift_state = "LTRS"
    for ch in text:
        candidate = ch.upper() if ch.isalpha() else ch
        in_ltrs = candidate in _LTRS_REVERSE
        in_figs = candidate in _FIGS_REVERSE
        if in_ltrs and in_figs:
            codes.append(_LTRS_REVERSE[candidate])
            if candidate == " ":
                shift_state = "LTRS"
            continue
        if in_ltrs:
            if shift_state != "LTRS":
                codes.append(LTRS_SHIFT)
                shift_state = "LTRS"
            codes.append(_LTRS_REVERSE[candidate])
        elif in_figs:
            if shift_state != "FIGS":
                codes.append(FIGS_SHIFT)
                shift_state = "FIGS"
            codes.append(_FIGS_REVERSE[candidate])
        else:
            if shift_state != "FIGS":
                codes.append(FIGS_SHIFT)
                shift_state = "FIGS"
            codes.append(_FIGS_REVERSE["?"])
    return codes


def _codes_to_frames(codes, stop_bits):
    """5-bit codes -> a flat list of (bit, duration_bit_periods) tuples:
    1 start bit (space, 1.0) + 5 data bits LSB-first (1.0 each) + 1 stop
    element (mark, `stop_bits` -- typically 1.5) per code, the standard
    asynchronous RTTY character frame."""
    frames = []
    for code in codes:
        frames.append((0, 1.0))
        for i in range(5):
            frames.append(((code >> i) & 1, 1.0))
        frames.append((1, stop_bits))
    return frames


def _preamble_frames(preamble_s, baud_rate):
    """Continuous idle mark for approximately preamble_s seconds at the
    given baud rate -- see module docstring's Preamble section for why
    this is deliberately NOT an alternating pattern."""
    n = max(0, round(preamble_s * baud_rate))
    return [(1, 1.0) for _ in range(n)]


def _synthesize_fsk(frames, sample_rate, mark_hz, space_hz, baud_rate, reverse, amplitude):
    """frames -> real audio samples via continuous-phase 2FSK synthesis
    (see module docstring) -- ONE running phase accumulator carried
    across every bit boundary in this single pass, never reset."""
    if not frames:
        return np.zeros(0, dtype=np.float32)
    bit_period_s = 1.0 / baud_rate
    seg_lengths = [max(1, round(dur * bit_period_s * sample_rate)) for _, dur in frames]
    out = np.zeros(sum(seg_lengths), dtype=np.float64)
    phase = 0.0
    idx = 0
    for (bit, _dur), n in zip(frames, seg_lengths):
        freq = _tone_for_bit(bit, mark_hz, space_hz, reverse)
        t = np.arange(n) / sample_rate
        seg_phase = phase + 2 * np.pi * freq * t
        out[idx:idx + n] = np.cos(seg_phase)
        phase = np.mod(phase + 2 * np.pi * freq * n / sample_rate, 2 * np.pi)
        idx += n
    return (out * amplitude).astype(np.float32)


def encode_text(text, sample_rate, mark_hz, shift_hz, baud_rate, reverse=False,
                 tail_s=0.2, preamble_s=1.0, stop_bits=1.5, amplitude=0.7):
    """Returns (audio: np.ndarray[float32], duration_s: float) -- same
    shape as psk31.encode_text()/digitext.encode_text(), suitable for
    feeding this app's existing Hilbert-based USB modulation chain (own
    dedicated instances, see pluto_tx/flowgraph.py's MODE_RTTY branch).

    space_hz is always mark_hz + shift_hz (shift is a magnitude, not a
    sign) -- `reverse` controls which physical tone plays which logical
    role, not which frequency is numerically higher.

    tail_s: true trailing silence appended after the signal, same
    rationale as psk31.py's/digitext.py's own tail_s."""
    if not text:
        text = " "
    space_hz = mark_hz + shift_hz
    codes = _text_to_baudot_codes(text)
    frames = _preamble_frames(preamble_s, baud_rate) + _codes_to_frames(codes, stop_bits)
    audio = _synthesize_fsk(frames, sample_rate, mark_hz, space_hz, baud_rate, reverse, amplitude)
    if tail_s > 0:
        audio = np.concatenate([audio, np.zeros(int(tail_s * sample_rate), dtype=np.float32)])
    return audio, len(audio) / sample_rate


def estimate_duration(text, baud_rate, tail_s=0.2, preamble_s=1.0, stop_bits=1.5):
    """Cheap preview estimate (bit-count only, no actual synthesis) for a
    live GUI readout while typing -- mirrors psk31.estimate_duration()."""
    if not text:
        text = " "
    codes = _text_to_baudot_codes(text)
    n_preamble_bits = max(0, round(preamble_s * baud_rate))
    n_bits = n_preamble_bits + len(codes) * (1 + 5 + stop_bits)
    return n_bits / baud_rate + tail_s
