"""Js8SequenceSource: one complex source for all frames of a JS8 transmission, each frame at its own start
time (consecutive periods, docs/js8/SPEC.md 3.1).

Why one source for the whole run instead of one Ft8TimedSource per frame: swapping a source into the
running graph (lock()/unlock()) takes 0.5-2.5 s on a Pluto, but consecutive JS8 frames are only 2.05-4.7 s
apart (period minus 79 symbols). So the source is swapped in once, before the first frame; every frame is
still keyed on its own (PlutoTxFlowgraph keys/unkeys the RF path around each frame, which needs no graph
lock), and between frames the RF path is dark.

Timing: silence until the wall clock reaches the first start (minus lead_s, like Ft8TimedSource); from then
on sample n is due at that moment + n / rate, so frame i begins exactly (start_i - start_0) * rate samples
after frame 0 -- sample-exact spacing on a paced device. The source never runs more than WAIT_AHEAD_S
ahead of the wall clock (an unpaced sink, e.g. the test fake, isn't flooded).

A frame's samples are handed in by load_frame(i, iq) (the flowgraph does that when it keys frame i, so the
drift pre-compensation can use the prediction for that moment and 20 SLOW frames need not sit in memory at
once). A frame that isn't loaded when it is due goes out as silence. cancel() ends the source at once."""
import threading
import time

import numpy as np
from gnuradio import gr

WAIT_AHEAD_S = 0.2


class Js8SequenceSource(gr.sync_block):
    def __init__(self, starts, frame_samples, sample_rate, lead_s=0.0, clock=time.time):
        gr.sync_block.__init__(self, name="Js8SequenceSource", in_sig=None, out_sig=[np.complex64])
        if not starts:
            raise ValueError("no frames")
        self._rate = float(sample_rate)
        self._clock = clock
        self._t0 = starts[0] - lead_s                      # wall time of the first frame's first sample
        self._offsets = [int(round((s - starts[0]) * self._rate)) for s in starts]
        self._frame_samples = int(frame_samples)            # every frame: 79 symbols + tail
        self._frames = {}
        self._lock = threading.Lock()
        self._cancelled = False
        self._n = None                                      # samples produced since _t0 (None = waiting)
        self._t_first = None
        self._zeros = 0
        self.frame_started_at = {}                          # frame index -> wall time of its first sample
        self.frames_sent = set()

    @property
    def num_frames(self):
        return len(self._offsets)

    def load_frame(self, i, iq):
        iq = np.asarray(iq, dtype=np.complex64)[:self._frame_samples]
        with self._lock:
            self._frames[i] = iq

    def cancel(self):
        with self._lock:
            self._cancelled = True
            self._frames.clear()

    @property
    def cancelled(self):
        return self._cancelled

    def _pace(self, due_at, want):
        """How many samples may be produced now (>= 1), the next one being due at wall time due_at."""
        while True:
            ahead = int((self._clock() + WAIT_AHEAD_S - due_at) * self._rate)
            if ahead >= 1 or self._cancelled:
                return max(1, min(want, ahead))
            time.sleep(min(0.05, (1 - ahead) / self._rate + 0.002))

    def work(self, input_items, output_items):
        out = output_items[0]
        if self._cancelled:
            return -1                                       # WORK_DONE
        if self._n is None:                                 # before the first frame: like Ft8TimedSource
            now = self._clock()
            if now < self._t0:
                if self._t_first is None:
                    self._t_first = now
                allowed = int((now - self._t_first + WAIT_AHEAD_S) * self._rate) - self._zeros
                if allowed < 1:
                    time.sleep((1 - allowed) / self._rate + 0.002)
                    allowed = 1
                k = max(1, min(len(out), allowed, int((self._t0 - now) * self._rate) + 1))
                out[:k] = 0
                self._zeros += k
                return k
            self._n = 0
        end = self._offsets[-1] + self._frame_samples
        if self._n >= end:
            return -1
        k = min(self._pace(self._t0 + self._n / self._rate, len(out)), end - self._n)
        out[:k] = 0
        with self._lock:
            for i, off in enumerate(self._offsets):
                iq = self._frames.get(i)
                if iq is None:
                    continue
                a, b = max(self._n, off), min(self._n + k, off + len(iq))
                if a < b:
                    out[a - self._n:b - self._n] = iq[a - off:b - off]
                    if i not in self.frame_started_at:
                        self.frame_started_at[i] = self._clock()
                    if b == off + len(iq):
                        self.frames_sent.add(i)
        self._n += k
        return k
