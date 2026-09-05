"""FreeDVEncoder: a custom gr.basic_block wrapping libcodec2's freedv_tx()
for FreeDV 2020/2020B TX. No suitable off-the-shelf GNU Radio block exists
for this -- gr-vocoder's built-in FreeDV blocks only cover legacy modes
(700/1600/2400) and are broken against modern codec2 (see README).

Unlike m17_coder (this app's other custom-block digital voice dependency),
freedv_tx() has no external start/stop state machine (no SOT/EOT) -- it's
simply called continuously on whatever speech audio is flowing; PTT gates
ptt_mute upstream exactly like FM/SSB, nothing block-internal needed.

int16 speech in (16kHz) -> exactly n_speech_samples per freedv_tx() call ->
int16 modulated audio out (8kHz), exactly n_nom_modem_samples per call.
Both FreeDV 2020 (2880->1440) and 2020B (1440->720) measured this session
at a clean 2:1 sample-count ratio, but the exact frame sizes differ between
modes -- explicitly declares the true ratio via set_relative_rate() so the
GNU Radio scheduler negotiates buffers correctly. This is a direct
application of the lesson from this session's m17_coder investigation: an
undeclared/wrong relative_rate on a frame-quantized block risks a scheduler
buffer-negotiation deadlock (confirmed as gr-m17's actual bug, upstream).
FreeDV's frame sizes here are far more modest than m17_coder's ~520x
downstream expansion and likely wouldn't trigger the same failure mode --
but declaring the rate correctly costs nothing and is simply correct.

Mode is fixed for the lifetime of one FreeDVEncoder instance (2020 vs
2020B have different frame sizes, and reconfiguring output_multiple/
relative_rate on a live, already-scheduled block mid-stream is uncertain,
unverified territory). Switching between 2020/2020B at runtime is instead
handled at the flowgraph level (PlutoTxFlowgraph.set_freedv_variant()):
tb.lock(), swap in a freshly constructed FreeDVEncoder for the new mode,
tb.unlock() -- the exact same lock()/connect()/disconnect() pattern this
session already proved safe on real hardware for M17's mode entry/exit.
"""
import threading

import numpy as np
from gnuradio import gr

from . import freedv_ctypes as fdv


class FreeDVEncoder(gr.basic_block):
    def __init__(self, mode=fdv.FREEDV_MODE_2020, text=""):
        gr.basic_block.__init__(
            self,
            name="freedv_encoder",
            in_sig=[np.int16],
            out_sig=[np.int16],
        )
        self._lock = threading.Lock()
        self.mode = mode
        self._session = fdv.FreeDVSession(mode)
        if text:
            self._session.set_text(text)
        self._n_speech = self._session.n_speech_samples
        self._n_modem = self._session.n_nom_modem_samples

        self.set_output_multiple(self._n_modem)
        # relative_rate = output_items / input_items, matches m17_coder's
        # set_relative_rate(SYM_PER_FRA, PAYLOAD_BYTES) pattern from this
        # session -- here it's exactly n_nom_modem_samples : n_speech_samples.
        self.set_relative_rate(self._n_modem, self._n_speech)

    def set_text(self, text: str):
        with self._lock:
            self._session.set_text(text)

    def forecast(self, noutput_items, ninputs):
        # NOTE: gnuradio.gr.gateway's Python forecast() signature differs
        # from the C++ gr::block::forecast() -- it takes the input PORT
        # COUNT (an int) and must RETURN a list of required item counts,
        # not mutate an in/out vector like the C++ API does.
        frames = max(1, noutput_items // self._n_modem)
        return [frames * self._n_speech] * ninputs

    def general_work(self, input_items, output_items):
        in0 = input_items[0]
        out0 = output_items[0]
        frames = min(len(in0) // self._n_speech, len(out0) // self._n_modem)
        if frames <= 0:
            self.consume_each(0)
            return 0

        with self._lock:
            session = self._session
            for i in range(frames):
                speech_chunk = in0[i * self._n_speech:(i + 1) * self._n_speech]
                out0[i * self._n_modem:(i + 1) * self._n_modem] = session.tx(speech_chunk)

        self.consume_each(frames * self._n_speech)
        return frames * self._n_modem

    def close(self):
        with self._lock:
            self._session.close()
