"""JS8 receive side for the RX app / CLI / Web-TRX: a GNU Radio sink that keeps the 12 kHz USB audio in a
ring buffer and decodes each period of each selected speed (Normal 15 s, Fast 10 s, Turbo 6 s, Slow
30 s -- several at once, each at its own UTC period boundaries) in a worker thread (js8_decoder), plus
Js8State, the Qt-free decode/message/station store the GUIs poll (lock-guarded mutate / snapshot like
ft8_rx.Ft8State).

Timing as in ft8_rx: every work() call stamps its newest sample with the wall clock (minus
config.JS8_RX_LATENCY_S); period [t, t + period) is decoded at t + period + config.JS8_DECODE_LAG_S."""
import threading
import time

import numpy as np
from gnuradio import gr

from pluto_tx import js8_phy

from . import config
from .js8_assembly import Js8Assembler
from .js8_decoder import SAMPLE_RATE, Js8SlotDecoder

MAX_ROWS = 2000
MAX_MESSAGES = 500


def _period(submode):
    return js8_phy.SUBMODES[submode]["period_s"]


class Js8Receiver(gr.sync_block):
    def __init__(self, on_decodes, submodes=config.JS8_DEFAULT_SUBMODES, backend=config.JS8_DECODER_BACKEND,
                 clock=time.time):
        gr.sync_block.__init__(self, name="Js8Receiver", in_sig=[np.float32], out_sig=None)
        self.submodes = tuple(sorted(set(submodes)))
        if not self.submodes or any(sm not in js8_phy.SUBMODES for sm in self.submodes):
            raise ValueError(f"invalid JS8 speeds {submodes!r}")
        self._on_decodes = on_decodes          # (slot_start, submode, [Js8Decode])
        self._clock = clock
        self._backend = backend
        self._lock = threading.Lock()
        longest = max(_period(sm) for sm in self.submodes)
        self._ring = np.zeros(int(2 * longest * SAMPLE_RATE) + SAMPLE_RATE, dtype=np.float32)
        self._written = 0
        self._t_last = None
        self._stop = threading.Event()
        self._thread = None
        self.decoder = None
        self.slots_decoded = 0
        self.last_error = ""

    # --- GNU Radio side ------------------------------------------------------------------------------
    def work(self, input_items, output_items):
        x = input_items[0]
        n = len(x)
        t = self._clock() - config.JS8_RX_LATENCY_S
        with self._lock:
            size = len(self._ring)
            if n >= size:
                self._ring[:] = x[-size:]
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
        self._thread = threading.Thread(target=self._run, name="js8-decode", daemon=True)
        self._thread.start()
        return True

    def stop(self):
        self._stop.set()
        return True

    # --- slot extraction + decoding ------------------------------------------------------------------
    def slot_audio(self, slot_start, period):
        out = np.zeros(int(period * SAMPLE_RATE), dtype=np.float32)
        with self._lock:
            if self._t_last is None:
                return out
            size = len(self._ring)
            newest = self._written - 1
            i0 = newest - int(round((self._t_last - slot_start) * SAMPLE_RATE))
            oldest = max(0, self._written - size)
            a = max(i0, oldest)
            b = min(i0 + len(out), self._written)
            if b <= a:
                return out
            idx = np.arange(a, b) % size
            out[a - i0:b - i0] = self._ring[idx]
        return out

    def first_slots(self, now):
        """{submode: start of the period in progress} -- the first period each speed decodes."""
        return {sm: (now - config.JS8_DECODE_LAG_S) // _period(sm) * _period(sm) for sm in self.submodes}

    @staticmethod
    def next_due(next_slots):
        """-> (due_time, slot_start, submode): the earliest pending period (ties: shortest period first)."""
        return min((slot + _period(sm) + config.JS8_DECODE_LAG_S, slot, sm) for sm, slot in next_slots.items())

    def _run(self):
        try:
            self.decoder = Js8SlotDecoder(self._backend, *config.JS8_BAND_HZ)
        except Exception as e:
            self.last_error = str(e)
            return
        next_slots = self.first_slots(self._clock())
        while not self._stop.is_set():
            due, slot, sm = self.next_due(next_slots)
            if self._stop.wait(max(0.0, due - self._clock())):
                break
            p = _period(sm)
            next_slots[sm] = slot + p
            behind = self._clock() - (next_slots[sm] + p + config.JS8_DECODE_LAG_S)
            if behind > 0:                                   # decoding fell behind: skip to the present
                next_slots[sm] += (behind // p + 1) * p
            audio = self.slot_audio(slot, p)
            if not np.any(audio):
                continue
            try:
                decodes = self.decoder.decode(audio, sm)
                self.last_error = ""
            except Exception as e:
                self.last_error = str(e)
                continue
            self.slots_decoded += 1
            self._on_decodes(slot, sm, decodes)
        self.decoder.close()


class Js8State:
    """Decoded JS8 frames, assembled messages and heard stations across flowgraph rebuilds."""

    def __init__(self, clock=time.time):
        self._lock = threading.Lock()
        self._clock = clock
        self._rows = []
        self._messages = {}               # id -> Js8Message
        self._assembler = Js8Assembler()
        self._version = 0
        self.slots = 0
        self.last_slot = None

    @property
    def version(self):
        with self._lock:
            return self._version

    def on_decodes(self, slot_start, submode, decodes):
        with self._lock:
            for d in decodes:
                self._rows.append(dict(slot=slot_start, submode=submode, snr=d.snr_db, dt=d.dt_s,
                                       freq=d.freq_hz, text=d.text, frame=d.frame, flags=d.flags))
            del self._rows[:-MAX_ROWS]
            for msg in self._assembler.feed(slot_start, submode, decodes):
                self._messages[msg.id] = msg
            for msg in self._assembler.expire(slot_start + js8_phy.SUBMODES[submode]["period_s"]):
                self._messages[msg.id] = msg
            while len(self._messages) > MAX_MESSAGES:
                self._messages.pop(next(iter(self._messages)))
            self.slots += 1
            self.last_slot = (slot_start, submode, len(decodes))
            self._version += 1

    def clear(self):
        with self._lock:
            self._rows.clear()
            self._messages.clear()
            self._assembler = Js8Assembler()
            self._version += 1

    def get_snapshot(self):
        """-> dict(version, rows, messages, stations); rows/messages oldest first, stations by last heard."""
        with self._lock:
            rows = [dict(r, utc=time.strftime("%H%M%S", time.gmtime(r["slot"]))) for r in self._rows]
            msgs = [dict(id=m.id, submode=m.submode, freq=m.freq_hz, first_slot=m.first_slot,
                         last_slot=m.last_slot, text=m.text, complete=m.complete, incomplete=m.incomplete,
                         closed=m.closed, snr=m.snr_db, sender=m.sender, to=m.to,
                         utc=time.strftime("%H%M%S", time.gmtime(m.first_slot)))
                    for m in self._messages.values()]
            stations = sorted((dict(vars(s)) for s in self._assembler.stations.values()),
                              key=lambda s: s["last_heard"], reverse=True)
            return dict(version=self._version, rows=rows, messages=msgs, stations=stations)
