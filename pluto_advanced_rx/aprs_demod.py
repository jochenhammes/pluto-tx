"""APRS demodulator: demodulated FM audio in, AX.25 frames that pass their FCS out. Numpy with a small Python bit loop, no
GNU Radio. Mark and Space energy come from correlators one bit long. Real senders differ a lot in how loud one tone is
against the other (pre-emphasis, mic filters), so several slicers with different weights run in parallel, each with its
own bit clock and HDLC state; a frame found by more than one of them counts once. Protocol: pluto_tx/aprs_codec.py.
"""
import time

import numpy as np

from pluto_tx import aprs_codec

BAUD = 1200
MARK_HZ = 1200.0
SPACE_HZ = 2200.0
SLICER_WEIGHTS = (0.0625, 0.25, 0.5, 1.0, 2.0, 4.0, 16.0)  # on power, Space against Mark: up to +-12 dB of tilt
_PLL_GAIN = 0.15
_DUPLICATE_S = 0.5


class _Slicer:
    """One bit clock and HDLC receiver."""

    def __init__(self, samples_per_bit, on_frame, weight):
        self.spb = samples_per_bit
        self.weight = weight
        self._on_frame = on_frame
        self._next = samples_per_bit / 2
        self._level = False
        self._prev_bit_tone = False
        self._ones = 0
        self._bits = []
        self._in_frame = False
        self._shift = 0

    def feed(self, soft):
        """soft > 0 means Mark. Called with consecutive blocks; positions are relative to each."""
        tone = soft > 0
        edges = np.flatnonzero(tone[1:] != tone[:-1]) + 1
        if self._level != bool(tone[0]):
            edges = np.concatenate(((0,), edges))
        n = len(tone)
        next_sample = self._next
        spb = self.spb
        for edge in edges.tolist():
            while next_sample < edge:
                self._bit(bool(tone[int(next_sample)]))
                next_sample += spb
            # An edge should fall half a bit from the sampling instant.
            error = (edge + spb / 2) - next_sample
            error = (error + spb / 2) % spb - spb / 2
            next_sample += _PLL_GAIN * error
        while next_sample < n:
            self._bit(bool(tone[int(next_sample)]))
            next_sample += spb
        self._next = next_sample - n
        self._level = bool(tone[-1])

    def _bit(self, level):
        bit = level == self._prev_bit_tone  # NRZI: no change is a 1
        self._prev_bit_tone = level
        self._shift = (self._shift << 1 | bit) & 0xFF
        if self._shift == 0x7E:
            if self._in_frame and len(self._bits) >= 8 * aprs_codec.MIN_FRAME_BYTES:
                self._finish()
            self._in_frame = True
            self._bits = []
            self._ones = 0
            return
        if not self._in_frame:
            return
        if bit:
            self._ones += 1
            if self._ones > 6:  # abort, or noise
                self._in_frame = False
                self._ones = 0
                return
            self._bits.append(1)
        else:
            if self._ones == 5:  # a stuffed 0
                self._ones = 0
                return
            self._ones = 0
            self._bits.append(0)
        if len(self._bits) > 8 * (aprs_codec.MAX_FRAME_BYTES + 2):
            self._in_frame = False

    def _finish(self):
        # The closing flag's first seven bits went into the buffer before it was recognised.
        bits = self._bits[:-7] if len(self._bits) >= 7 else []
        if len(bits) % 8:
            return
        data = np.packbits(np.array(bits, dtype=np.uint8), bitorder="little").tobytes()
        self._on_frame(data)


class AprsFrameDetector:
    """feed() demodulated audio at `sample_rate`; on_frame(frame_dict) gets each frame passing its FCS, once."""

    def __init__(self, sample_rate, on_frame):
        self.sample_rate = float(sample_rate)
        self._on_frame = on_frame
        self._window = max(2, int(round(self.sample_rate / BAUD)))
        self._mark = 2j * np.pi * MARK_HZ / self.sample_rate
        self._space = 2j * np.pi * SPACE_HZ / self.sample_rate
        self._n = 0
        self._tail_mark = np.zeros(self._window, np.complex64)
        self._tail_space = np.zeros(self._window, np.complex64)
        self._slicers = [_Slicer(self.sample_rate / BAUD, self._candidate, w) for w in SLICER_WEIGHTS]
        self._recent = {}
        self.frames_ok = 0
        self.fcs_failures = 0

    def _candidate(self, data):
        frame = aprs_codec.parse_frame(data)
        if frame is None:
            self.fcs_failures += 1
            return
        now = time.monotonic()
        self._recent = {k: t for k, t in self._recent.items() if now - t < _DUPLICATE_S}
        if data in self._recent:
            return
        self._recent[data] = now
        self.frames_ok += 1
        self._on_frame(frame)

    def _correlate(self, x, phase_step, tail):
        t = np.arange(self._n, self._n + len(x))
        mixed = np.concatenate((tail, x * np.exp(-phase_step * t).astype(np.complex64)))
        cumulative = np.concatenate(((0,), np.cumsum(mixed, dtype=np.complex128)))
        window = self._window
        out = cumulative[window:] - cumulative[:-window]
        return np.abs(out[-len(x):]) ** 2, mixed[-window:]

    def feed(self, samples):
        x = np.asarray(samples, dtype=np.float32)
        if x.size == 0:
            return
        mark, self._tail_mark = self._correlate(x, self._mark, self._tail_mark)
        space, self._tail_space = self._correlate(x, self._space, self._tail_space)
        self._n += len(x)
        for slicer in self._slicers:
            slicer.feed(mark - slicer.weight * space)
