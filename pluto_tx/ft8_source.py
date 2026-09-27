"""Ft8TimedSource: a one-shot complex source that holds the FT8 signal back until a wall-clock start
time, emitting silence until then.

Why not a vector_source with leading zeros: swapping a source into the running graph needs
lock()/unlock(), and on a Pluto the unlock() alone takes 0.5-2 s (the sink restarts) -- measured
against a time-stamped RTL-SDR capture, the signal then started 0.5-2 s late, and differently each
time. This block decides from the clock at the moment its samples are produced, so however long the
swap took, the signal goes out at start_at (plus the small, constant buffering between here and the
antenna, config.FT8_TX_LATENCY_S, which the caller compensates).

While waiting it never runs more than WAIT_AHEAD_S ahead of real time, so an unpaced sink (the test
fake) isn't flooded with silence; a real device paces the flow anyway. After the signal it returns
WORK_DONE, finishing like a one-shot vector_source."""
import time

import numpy as np
from gnuradio import gr

WAIT_AHEAD_S = 0.2


class Ft8TimedSource(gr.sync_block):
    def __init__(self, iq, sample_rate, start_at=None, lead_s=0.0, clock=time.time):
        gr.sync_block.__init__(self, name="Ft8TimedSource", in_sig=None, out_sig=[np.complex64])
        self._iq = np.asarray(iq, dtype=np.complex64)
        self._rate = float(sample_rate)
        self._switch_at = None if start_at is None else start_at - lead_s
        self._clock = clock
        self._pos = 0
        self._zeros = 0
        self._t_first = None
        self.signal_started_at = None     # wall time the first signal sample was produced

    def work(self, input_items, output_items):
        out = output_items[0]
        if self._switch_at is not None:
            now = self._clock()
            if now < self._switch_at:
                if self._t_first is None:
                    self._t_first = now
                allowed = int((now - self._t_first + WAIT_AHEAD_S) * self._rate) - self._zeros
                if allowed < 1:
                    # never return 0 from a source (the scheduler would not call it again): wait
                    # until at least one more sample of silence is due instead
                    time.sleep((1 - allowed) / self._rate + 0.002)
                    allowed = 1
                k = max(1, min(len(out), allowed, int((self._switch_at - now) * self._rate) + 1))
                out[:k] = 0
                self._zeros += k
                return k
            self._switch_at = None
        if self._pos >= len(self._iq):
            return -1   # WORK_DONE
        if self.signal_started_at is None:
            self.signal_started_at = self._clock()
        n = min(len(out), len(self._iq) - self._pos)
        out[:n] = self._iq[self._pos:self._pos + n]
        self._pos += n
        return n
