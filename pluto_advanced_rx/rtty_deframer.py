"""GNU Radio sink block that decodes RTTY (Baudot/ITA2, 2-tone FSK) text
directly out of a sliced bit stream (1 bit per byte, unpacked, values 0/1
-- the output of digital.binary_slicer_fb() fed by an oversampled
quadrature-FM-discriminator chain, see pluto_advanced_rx/flowgraph.py's
RTTY branch) and invokes a plain Python callback per decoded character.
Same "gr.sync_block with out_sig=None, Python callback/poll surface"
idiom already proven by psk31_deframer.py's PSK31VaricodeDeframer.

## Why this is NOT symbol_sync_ff + a bit-pattern scanner, unlike PSK31

PSK31's "idle" is a disguised *synchronous* bitstream (idle transmits a
continuous run of real Varicode space characters -- symbol_sync_ff's
Mueller & Mueller timing-error detector always has real transitions to
lock onto). Classic Baudot/RTTY is genuinely *asynchronous*: idle is
continuous mark with ZERO transitions for an arbitrary, non-bit-aligned
duration between characters -- a continuously-running M&M loop coasting
through that gap drifts, and it's exactly the next start bit (which
would set the sample instants for all 5 following data bits) where that
drift bites hardest.

Fix used here: the upstream chain feeds this block an OVERSAMPLED bit
stream (many raw samples per nominal bit period, see
RTTY_WORKING_RATE_HZ/baud_rate in pluto_advanced_rx/config.py), and this
block does classic open-loop UART-style framing itself: watch for a
mark->space edge (a start bit) after idle, then sample the middle of
each subsequent data/stop-bit slot at FIXED offsets computed from
working_rate_hz/baud_rate (recomputed instantly on a baud-rate change,
via set_baud_rate() -- no GNU Radio block reconfiguration needed).

## Baudot/ITA2 table and Normal/Reverse

Baudot/ITA2 table, shift codes, and bit order are a SEPARATE,
independent copy of pluto_tx/rtty.py's encoder-side table -- matches
this project's established convention for protocol/PHY logic that must
stay byte-for-byte in sync across the two independent app packages (see
psk31_deframer.py's own docstring). If you change the table, change
BOTH copies.

The upstream slicer's raw bit `s` reflects pure physics (1 = the
demodulated instantaneous frequency was closer to the higher SPACE
tone, 0 = closer to the lower MARK tone), independent of Normal/Reverse.
`_logical_bit()` maps that to this module's own bit convention (1=MARK/
idle, 0=SPACE/start -- matching pluto_tx/rtty.py's `_tone_for_bit()`
convention exactly) according to `self._reverse`, so toggling
Normal/Reverse is a cheap attribute flip (`set_reverse()`), not a GNU
Radio reconfiguration."""
import threading

from gnuradio import gr
import numpy as np

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


class RTTYBaudotDeframer(gr.sync_block):
    def __init__(self, on_char, working_rate_hz, baud_rate, stop_bits=1.5, reverse=False):
        gr.sync_block.__init__(self, name="RTTYBaudotDeframer", in_sig=[np.uint8], out_sig=None)
        self._on_char = on_char
        self._lock = threading.Lock()
        self._working_rate_hz = float(working_rate_hz)
        self._stop_bits = float(stop_bits)
        self._reverse = bool(reverse)
        self._baud_rate = float(baud_rate)
        self._samples_per_bit = self._working_rate_hz / self._baud_rate
        self._chars_decoded = 0
        self._sample_counter = 0
        self._prev_bit = 1  # assume idle/mark at startup -- self-corrects on the first real edge
        self._shift = "LTRS"
        self._state_framing = False
        self._frame_start = 0
        self._collected = []
        self._targets = []
        self._stop_target = 0
        self._target_idx = 0

    def _logical_bit(self, raw_bit):
        """raw_bit: 1 = space-tone dominant (physics), 0 = mark-tone
        dominant. See module docstring's Normal/Reverse section."""
        return raw_bit if self._reverse else (1 - raw_bit)

    def work(self, input_items, output_items):
        raw_bits = input_items[0]
        with self._lock:
            for raw in raw_bits:
                bit = self._logical_bit(int(raw) & 1)
                self._sample_counter += 1
                if not self._state_framing:
                    if self._prev_bit == 1 and bit == 0:
                        # mark->space edge: a start bit begins at this sample.
                        self._state_framing = True
                        self._frame_start = self._sample_counter
                        self._collected = []
                        self._targets = [
                            self._frame_start + round((1.5 + i) * self._samples_per_bit) for i in range(5)
                        ]
                        self._stop_target = self._frame_start + round(
                            (6.0 + self._stop_bits / 2.0) * self._samples_per_bit
                        )
                        self._target_idx = 0
                elif self._target_idx < 5:
                    if self._sample_counter >= self._targets[self._target_idx]:
                        self._collected.append(bit)
                        self._target_idx += 1
                elif self._sample_counter >= self._stop_target:
                    if bit == 1 and len(self._collected) == 5:
                        code = 0
                        for i, b in enumerate(self._collected):
                            code |= b << i
                        self._decode_code(code)
                    # else: framing error (bad stop bit) -- silently discard
                    # and resync on the next edge, same "no preamble-skip
                    # logic needed" spirit as PSK31's deframer.
                    self._state_framing = False
                self._prev_bit = bit
        return len(raw_bits)

    def _decode_code(self, code):
        if code == LTRS_SHIFT:
            self._shift = "LTRS"
            return
        if code == FIGS_SHIFT:
            self._shift = "FIGS"
            return
        table = _LTRS_TABLE if self._shift == "LTRS" else _FIGS_TABLE
        ch = table[code]
        if ch == "\0":
            return
        if ch == " ":
            self._shift = "LTRS"  # USOS: Unshift On Space
        self._chars_decoded += 1
        self._on_char(ch)

    def set_baud_rate(self, baud_rate: float):
        with self._lock:
            self._baud_rate = float(baud_rate)
            self._samples_per_bit = self._working_rate_hz / self._baud_rate

    def set_reverse(self, reverse: bool):
        with self._lock:
            self._reverse = bool(reverse)

    @property
    def chars_decoded(self):
        with self._lock:
            return self._chars_decoded
