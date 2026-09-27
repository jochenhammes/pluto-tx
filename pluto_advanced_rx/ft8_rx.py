"""FT8 receive side for the RX app: a GNU Radio sink that cuts the 12 kHz USB audio into 15 s UTC
slots and decodes each one in a worker thread (ft8_decoder.Ft8SlotDecoder -- jt9 or ft8_lib), plus
Ft8State, the Qt/GNU-Radio-free decode list the GUI polls (same lock-guarded mutate / snapshot
pattern as pocsag_state.py).

Slot timing comes from the wall clock: every work() call stamps its last sample with time.time()
(minus config.FT8_RX_LATENCY_S), so a sample's time is known to within the device's buffering. The
decode for slot [t, t+15) runs at t + FT8_DECODE_AT_S -- after the latest possible signal end -- on
whatever audio covers that span (gaps, e.g. while the graph was paused, stay zero)."""
import threading
import time

import numpy as np
from gnuradio import gr

from . import config
from .ft8_decoder import SAMPLE_RATE, SLOT_S, Ft8SlotDecoder

MAX_ROWS = 2000


class Ft8Receiver(gr.sync_block):
    def __init__(self, on_decodes, backend="auto", clock=time.time, my_call="", my_grid=""):
        gr.sync_block.__init__(self, name="Ft8Receiver", in_sig=[np.float32], out_sig=None)
        self._on_decodes = on_decodes
        self._clock = clock
        self._backend = backend
        self._my = (my_call, my_grid)
        self._lock = threading.Lock()
        n = int(2 * SLOT_S * SAMPLE_RATE)
        self._ring = np.zeros(n, dtype=np.float32)
        self._written = 0            # total samples ever written
        self._t_last = None          # wall time of the newest sample
        self._stop = threading.Event()
        self._thread = None
        self.decoder = None
        self.slots_decoded = 0
        self.last_error = ""

    # --- GNU Radio side --------------------------------------------------------------------------
    def work(self, input_items, output_items):
        x = input_items[0]
        n = len(x)
        t = self._clock() - config.FT8_RX_LATENCY_S
        with self._lock:
            size = len(self._ring)
            if n >= size:
                self._ring[:] = x[-size:]
                self._written += n
            else:
                start = self._written % size
                first = min(n, size - start)
                self._ring[start:start + first] = x[:first]
                self._ring[:n - first] = x[first:]
                self._written += n
            self._t_last = t
        return n

    def start(self):
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="ft8-decode", daemon=True)
        self._thread.start()
        return True

    def stop(self):
        self._stop.set()
        return True

    # --- slot extraction + decoding ----------------------------------------------------------------
    def slot_audio(self, slot_start):
        """The audio of [slot_start, slot_start + 15 s) as far as it is still in the ring."""
        out = np.zeros(int(SLOT_S * SAMPLE_RATE), dtype=np.float32)
        with self._lock:
            if self._t_last is None:
                return out
            size = len(self._ring)
            newest = self._written - 1                        # absolute index of the newest sample
            i0 = newest - int(round((self._t_last - slot_start) * SAMPLE_RATE))
            oldest = max(0, self._written - size)
            a = max(i0, oldest)
            b = min(i0 + len(out), self._written)
            if b <= a:
                return out
            idx = np.arange(a, b) % size
            out[a - i0:b - i0] = self._ring[idx]
        return out

    def _run(self):
        try:
            self.decoder = Ft8SlotDecoder(self._backend, my_call=self._my[0], my_grid=self._my[1])
        except Exception as e:
            self.last_error = str(e)
            return
        while not self._stop.is_set():
            now = self._clock()
            slot = (now - config.FT8_DECODE_AT_S) // SLOT_S * SLOT_S + SLOT_S
            if self._stop.wait(max(0.0, slot + config.FT8_DECODE_AT_S - self._clock())):
                break
            audio = self.slot_audio(slot)
            if not np.any(audio):
                continue
            try:
                decodes = self.decoder.decode(audio)
                self.last_error = ""
            except Exception as e:
                self.last_error = str(e)
                continue
            self.slots_decoded += 1
            self._on_decodes(slot, decodes)
        self.decoder.close()


class Ft8State:
    """Decoded FT8 messages across flowgraph rebuilds (owned by the GUI / CLI)."""

    def __init__(self):
        self._lock = threading.Lock()
        self._rows = []
        self._version = 0
        self.slots = 0
        self.last_slot = None

    @property
    def version(self):
        with self._lock:
            return self._version

    def on_decodes(self, slot_start, decodes):
        with self._lock:
            for d in decodes:
                self._rows.append(dict(slot=slot_start, snr=d.snr_db, dt=d.dt_s, freq=d.freq_hz, text=d.text))
            del self._rows[:-MAX_ROWS]
            self.slots += 1
            self.last_slot = (slot_start, len(decodes))
            self._version += 1

    def clear(self):
        with self._lock:
            self._rows.clear()
            self._version += 1

    def get_snapshot(self):
        """-> (version, rows) with rows as dicts: utc (HHMMSS), snr, dt, freq, text; oldest first."""
        with self._lock:
            rows, version = list(self._rows), self._version
        return version, [dict(r, utc=time.strftime("%H%M%S", time.gmtime(r["slot"]))) for r in rows]
