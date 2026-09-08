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
everything FFT-related (ring buffer size, FFT length, compute throttling,
zoom, averaging) is purely internal Python state guarded by a lock --
runtime changes (set_fft_size/set_zoom/set_avg_count) never touch GNU
Radio's block/scheduler machinery at all, so none of them need a flowgraph
rebuild.

Zoom (set_zoom): a "zoom-FFT" technique, not a separate time-domain
decimation filter ahead of this block (unlike pluto_tx's waterfall, which
IS a GNU Radio rational_resampler_ccf, fixed span, needs no runtime
adjustment). Here the ring buffer holds fft_size*zoom raw samples; a single
FFT of that full (larger) length is computed, then only the CENTER
fft_size bins are kept. That crop is mathematically equivalent to
decimating the time-domain signal by `zoom` through a perfect brick-wall
low-pass filter first (a standard technique for spectral zooming) --
without needing a separate filter design, a GNU Radio block, or (crucially)
a flowgraph rebuild: zoom can change live, on every slider tick, at
whatever rate the GUI thread calls set_zoom().

Averaging (set_avg_count): a rolling mean of the last N computed spectra,
in POWER (not dB, and not raw amplitude) -- averaging in the dB domain is
a common but real mistake (it isn't a linear quantity); averaging power
(|X|^2) then converting the mean to dB at the end is the mathematically
correct "video averaging" a real spectrum analyzer does.
"""
import collections
import threading

import numpy as np
from gnuradio import gr
from gnuradio.fft import window as gr_window


class FftProbe(gr.sync_block):
    # Zoom bound: effective FFT length is fft_size*zoom, so this keeps the
    # worst case (largest FFT_SIZE_PRESETS entry times max zoom) bounded to
    # something still comfortably real-time at FFT_COMPUTE_RATE_HZ on
    # ordinary hardware, rather than unbounded.
    MAX_ZOOM = 32
    MAX_AVG = 100

    def __init__(self, fft_size, sample_rate, window_type, compute_rate_hz):
        gr.sync_block.__init__(self, name="FftProbe", in_sig=[np.complex64], out_sig=None)
        self._window_type = window_type
        self._compute_rate_hz = compute_rate_hz
        self._lock = threading.Lock()
        self._latest_row = None
        self._generation = 0
        self._zoom = 1
        self._avg_history = collections.deque(maxlen=1)
        self._configure(fft_size, sample_rate)

    def _configure(self, fft_size, sample_rate):
        """(Re)size the ring buffer / FFT window / compute throttle. Must be
        called with self._lock held, except from __init__ (nothing else can
        be running yet there). Invalidates the averaging history: its
        entries are only meaningful for the fft_size/zoom/sample_rate they
        were computed under (both their array shape via fft_size, and their
        per-bin frequency meaning via zoom/sample_rate)."""
        self._fft_size = fft_size
        self._sample_rate = sample_rate
        eff_size = fft_size * self._zoom
        self._eff_size = eff_size
        self._taps = np.array(gr_window.build(self._window_type, eff_size), dtype=np.float32)
        # Ring buffer holds a bit more than one (possibly zoomed) FFT's
        # worth so work() can always find a contiguous eff_size run ending
        # at the write cursor without a wraparound special case.
        self._ring = np.zeros(eff_size * 2, dtype=np.complex64)
        self._write_pos = 0
        self._since_last_fft = 0
        # Decouple "how often we have enough raw samples" from "how often we
        # actually compute an FFT" -- see the module docstring's original
        # reasoning; eff_size (not fft_size) is what actually gates a
        # zoomed-in FFT's minimum cadence.
        self._compute_stride = max(eff_size, int(sample_rate / self._compute_rate_hz))
        # Center fft_size bins of the eff_size-point FFT -- see the module
        # docstring's zoom-FFT explanation.
        self._crop_lo = (eff_size - fft_size) // 2
        self._crop_hi = self._crop_lo + fft_size
        self._avg_history.clear()

    def work(self, input_items, output_items):
        in0 = input_items[0]
        n = len(in0)
        with self._lock:
            eff_size = self._eff_size
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
                # The eff_size samples immediately preceding the write cursor.
                start = (self._write_pos - eff_size) % cap
                if start + eff_size <= cap:
                    chunk = ring[start:start + eff_size]
                else:
                    chunk = np.concatenate((ring[start:], ring[:eff_size - (cap - start)]))
                spec_full = np.fft.fftshift(np.fft.fft(chunk * self._taps))
                spec = spec_full[self._crop_lo:self._crop_hi] if self._zoom > 1 else spec_full
                power = (np.abs(spec) ** 2).astype(np.float32)
                self._avg_history.append(power)
                avg_power = np.mean(self._avg_history, axis=0) if len(self._avg_history) > 1 else power
                mag_db = (10.0 * np.log10(avg_power + 1e-12)).astype(np.float32)
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

    def set_zoom(self, zoom: int):
        """1 = full span (no zoom, the previous fixed behavior). Effective
        displayed span becomes sample_rate/zoom -- see the module
        docstring's zoom-FFT explanation. Takes effect immediately on the
        next computed row, no flowgraph rebuild needed (unlike an RX
        bandwidth change)."""
        with self._lock:
            zoom = max(1, min(int(zoom), self.MAX_ZOOM))
            if zoom == self._zoom:
                return
            self._zoom = zoom
            self._configure(self._fft_size, self._sample_rate)

    def set_avg_count(self, n: int):
        """1 = no averaging (each row is a single fresh spectrum)."""
        with self._lock:
            n = max(1, min(int(n), self.MAX_AVG))
            self._avg_history = collections.deque(self._avg_history, maxlen=n)

    @property
    def zoom(self):
        with self._lock:
            return self._zoom

    @property
    def fft_size(self):
        with self._lock:
            return self._fft_size
