"""A pure Python/numpy gr.sync_block that exposes the latest FFT magnitude
row of a complex stream to a polling GUI thread.

Why this exists: GNU Radio's own qtgui blocks (qtgui.waterfall_sink_c etc.,
what pluto_rx uses) render into an opaque C++/Qt widget with no data-out
port at all (confirmed by introspecting qtgui.sink_c: only qwidget()/
set_fft_size(), no pmt output). To build a custom interactive waterfall
(pyqtgraph-based, see waterfall_widget.py) the raw spectrum data has to come
out of the flowgraph some other way -- this block computes it in Python.

Deliberately does NOT use gr.block.set_output_multiple() at runtime, even
though it would be the "natural" way to express "give me fft_size samples
at a time". Reasoning (from gnuradio/block.h): a block's stream buffer is
sized once at connect()/start() time based on the output_multiple in effect
at that moment; raising it later can leave the scheduler permanently unable
to satisfy the "enough items available" check for this block (a silent
stall, not a crash), and d_output_multiple has no documented thread-safety
for a later GUI-thread write racing the scheduler thread's reads. Instead,
output_multiple stays at GNU Radio's default (1, unconstrained) and
everything FFT-related (ring buffer size, FFT length, compute throttling)
is purely internal Python state guarded by a lock -- runtime changes
(set_fft_size) never touch GNU Radio's block/scheduler machinery at all.
"""
import threading

import numpy as np
from gnuradio import gr
from gnuradio.fft import window as gr_window


class FftProbe(gr.sync_block):
    def __init__(self, fft_size, sample_rate, window_type, compute_rate_hz):
        gr.sync_block.__init__(self, name="FftProbe", in_sig=[np.complex64], out_sig=None)
        self._window_type = window_type
        self._compute_rate_hz = compute_rate_hz
        self._lock = threading.Lock()
        self._latest_row = None
        self._generation = 0
        self._configure(fft_size, sample_rate)

    def _configure(self, fft_size, sample_rate):
        """(Re)size the ring buffer / FFT window / compute throttle. Must be
        called with self._lock held, except from __init__ (nothing else can
        be running yet there)."""
        self._fft_size = fft_size
        self._sample_rate = sample_rate
        self._taps = np.array(gr_window.build(self._window_type, fft_size), dtype=np.float32)
        # Ring buffer holds a bit more than one FFT's worth so work() can
        # always find a contiguous fft_size run ending at the write cursor
        # without a wraparound special case.
        self._ring = np.zeros(fft_size * 2, dtype=np.complex64)
        self._write_pos = 0
        self._since_last_fft = 0
        # Decouple "how often we have enough raw samples" from "how often we
        # actually compute an FFT" -- at e.g. 2.5 Msps with fft_size=1024,
        # computing every fft_size-sample chunk would mean ~2440 FFTs/sec,
        # far more than any display needs. Cap the real compute rate instead.
        self._compute_stride = max(fft_size, int(sample_rate / self._compute_rate_hz))

    def work(self, input_items, output_items):
        in0 = input_items[0]
        n = len(in0)
        with self._lock:
            fft_size = self._fft_size
            ring = self._ring
            cap = len(ring)
            if n >= cap:
                # Pathological case (huge work buffer): only the tail matters.
                ring[:] = in0[-cap:]
                self._write_pos = 0
            else:
                end = self._write_pos + n
                if end <= cap:
                    ring[self._write_pos:end] = in0
                else:
                    first = cap - self._write_pos
                    ring[self._write_pos:] = in0[:first]
                    ring[:end - cap] = in0[first:]
                self._write_pos = end % cap

            self._since_last_fft += n
            if self._since_last_fft >= self._compute_stride:
                self._since_last_fft = 0
                # The fft_size samples immediately preceding the write cursor.
                start = (self._write_pos - fft_size) % cap
                if start + fft_size <= cap:
                    chunk = ring[start:start + fft_size]
                else:
                    chunk = np.concatenate((ring[start:], ring[:fft_size - (cap - start)]))
                spec = np.fft.fftshift(np.fft.fft(chunk * self._taps))
                mag_db = (20.0 * np.log10(np.abs(spec) + 1e-12)).astype(np.float32)
                self._latest_row = mag_db
                self._generation += 1
        return n

    def get_latest_row(self, since_generation=-1):
        """Returns (row, generation) if a new row exists since
        since_generation, else (None, current_generation). The returned row
        is always a fresh copy, safe to use outside the lock."""
        with self._lock:
            if self._generation == since_generation or self._latest_row is None:
                return None, self._generation
            return self._latest_row.copy(), self._generation

    def set_fft_size(self, fft_size):
        with self._lock:
            self._configure(fft_size, self._sample_rate)

    def set_sample_rate(self, sample_rate):
        with self._lock:
            self._configure(self._fft_size, sample_rate)

    @property
    def fft_size(self):
        with self._lock:
            return self._fft_size
