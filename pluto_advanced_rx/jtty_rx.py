"""JTTY receive side for the RX app / CLI / Web-TRX: a GNU Radio sink that streams the 12 kHz USB audio into one
wsjt-codec process (pluto_tx/jtty_codec.py, docs/jtty/SPEC.md). What it decodes goes to jtty_state.JttyState.

No slots and no clock in the protocol: a message's time only comes from where its samples were in the stream, mapped
to the wall clock here. The GNU Radio thread never touches the codec, which can stall (measuring FFT plans, a busy
decoder): a bounded queue goes to a writer thread, and what doesn't fit is dropped, counted and announced to the
codec as a discontinuity. An unexpected end of the codec restarts it once; after that last_error says so."""
import collections
import threading
import time

import numpy as np
from gnuradio import gr

from pluto_tx import jtty_codec

from . import config

SAMPLE_RATE = jtty_codec.SAMPLE_RATE
MAX_QUEUE_BYTES = 2 * SAMPLE_RATE * config.JTTY_QUEUE_S
TARGET_RMS = 3000.0                  # int16 units; int16 audio must not lose a weak signal to rounding
LEVEL_DECAY_S = 2.0                  # the level rises at once (no clipping when a strong signal starts) and falls slowly
MIN_LEVEL = 1e-6


class JttyReceiver(gr.sync_block):
    def __init__(self, on_update, rxfreq_hz=config.JTTY_RX_FREQ_HZ, tolerance_hz=config.JTTY_RX_TOLERANCE_HZ,
                 band_hz=config.JTTY_SEARCH_BAND_HZ, clock=time.time, stream_factory=jtty_codec.JttyStream):
        gr.sync_block.__init__(self, name="JttyReceiver", in_sig=[np.float32], out_sig=None)
        self._on_update = on_update    # (dict): jtty_codec's jtty_update line plus "utc_start" (epoch s)
        self._search = (int(rxfreq_hz), int(tolerance_hz), int(band_hz[0]), int(band_hz[1]))
        self._clock = clock
        self._stream_factory = stream_factory
        self._lock = threading.Lock()
        self._wake = threading.Condition(self._lock)
        self._queue = collections.deque()
        self._queued_bytes = 0
        self._gap = False
        self._stopping = True
        self._thread = None
        self._stream = None
        self._level = None
        self._written = 0              # samples handed to the codec
        self._t_last = None            # wall clock of the newest of them
        self.dropped_samples = 0
        self.restarts = 0
        self.last_error = ""

    # --- GNU Radio side ------------------------------------------------------------------------------
    def work(self, input_items, output_items):
        x = input_items[0]
        n = len(x)
        if n == 0:
            return 0
        rms = float(np.sqrt(np.mean(x * x)))
        if self._level is None or rms > self._level:
            self._level = rms
        else:
            self._level += (rms - self._level) * min(1.0, n / (LEVEL_DECAY_S * SAMPLE_RATE))
        gain = TARGET_RMS / max(self._level, MIN_LEVEL)
        pcm = (x * gain).clip(-32767.0, 32767.0).astype("<i2").tobytes()
        t = self._clock() - config.JTTY_RX_LATENCY_S
        with self._lock:
            if self._queued_bytes + len(pcm) > MAX_QUEUE_BYTES:
                self.dropped_samples += n
                self._gap = True
            else:
                self._queue.append((pcm, t))
                self._queued_bytes += len(pcm)
                self._wake.notify()
        return n

    def start(self):
        with self._lock:
            self._stopping = False
        try:
            self._open_stream()
        except (OSError, RuntimeError) as e:
            self.last_error = str(e)
        self._thread = threading.Thread(target=self._write_loop, name="jtty-write", daemon=True)
        self._thread.start()
        return True

    def stop(self):
        with self._lock:
            self._stopping = True
            self._wake.notify()
        if self._thread is not None:
            self._thread.join(timeout=5.0)
            self._thread = None
        stream, self._stream = self._stream, None
        if stream is not None:
            stream.close()
        return True

    def set_search(self, rxfreq_hz=None, tolerance_hz=None):
        """Where the decoder looks; it does not search the whole band (docs/jtty/SPEC.md)."""
        rx, tol, lo, hi = self._search
        self._search = (rx if rxfreq_hz is None else int(rxfreq_hz), tol if tolerance_hz is None else int(tolerance_hz), lo, hi)
        stream = self._stream
        if stream is not None:
            try:
                stream.configure(*self._search)
            except (OSError, ValueError) as e:
                self.last_error = str(e)

    # --- codec side ----------------------------------------------------------------------------------
    def _open_stream(self):
        self._stream = self._stream_factory(self._on_event, self._on_exit)
        self._stream.configure(*self._search)

    def _write_loop(self):
        while True:
            with self._lock:
                while not self._queue and not self._stopping:
                    self._wake.wait()
                if self._stopping and not self._queue:
                    return
                pcm, t = self._queue.popleft()
                self._queued_bytes -= len(pcm)
                gap, self._gap = self._gap, False
                stream = self._stream
            if stream is None:
                continue
            try:
                if gap:
                    stream.control({"t": "discontinuity"})
                stream.send_audio(pcm)
            except (OSError, ValueError):  # the pipe broke: _on_exit decides what happens next
                continue
            with self._lock:
                self._written += len(pcm) // 2
                self._t_last = t

    def _on_event(self, event):
        kind = event.get("t")
        if kind == "jtty_update":
            with self._lock:
                t_last, written = self._t_last, self._written
            start = event.get("start")
            event["utc_start"] = None if t_last is None or start is None else t_last - (written / SAMPLE_RATE - start)
            self._on_update(event)
        elif kind == "error":
            self.last_error = event.get("detail") or event.get("code") or event.get("msg") or "wsjt-codec error"

    def _on_exit(self, code, expected):
        if expected:
            return
        with self._lock:
            stopping = self._stopping
        if stopping:
            return
        if self.restarts >= 1:
            self.last_error = f"wsjt-codec stopped (exit {code}); JTTY reception has ended"
            self._stream = None
            return
        self.restarts += 1
        self.last_error = f"wsjt-codec stopped (exit {code}) and was restarted"
        try:
            self._open_stream()
        except (OSError, RuntimeError) as e:
            self.last_error = str(e)
            self._stream = None
