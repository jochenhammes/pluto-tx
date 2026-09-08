"""RadeEncoder: a custom gr.basic_block wrapping the two-stage RADE V1 TX
pipeline (lpcnet_demo speech->features, then librade.so's rade_tx()
features->IQ) verified end-to-end offline this session (Phase F/H of the
RADE integration plan): int16 speech in (16kHz) -> complex64 IQ out (8kHz).

12 lpcnet_demo frames (1920 PCM samples, 160 each) batch into one
432-float features_in for rade_tx(), producing 960 IQ samples -- that
1920:960 ratio is the block's true i/o rate, declared via
set_relative_rate(), the same discipline FreeDVEncoder/m17_coder already
established in this codebase (an undeclared/wrong relative_rate on a
frame-quantized block risks a scheduler buffer-negotiation deadlock,
confirmed as gr-m17's actual upstream bug).

send_eoo() is NOT part of the streaming work() path -- it's a one-shot
call the flowgraph makes on unkey (when EOO is enabled), mirroring how
M17's own EOT tail is handled at the flowgraph level, not inside the
coder block itself.
"""
import threading

import numpy as np
from gnuradio import gr

from . import rade_ctypes as rade
from .lpcnet_subprocess import LpcnetDemoProcess, MODE_FEATURES, LPCNET_FRAME_SIZE, NB_TOTAL_FEATURES

LPCNET_FRAMES_PER_RADE_FRAME = 12  # rade_n_features_in_out() (432) / NB_TOTAL_FEATURES (36)


class RadeEncoder(gr.basic_block):
    def __init__(self):
        gr.basic_block.__init__(
            self, name="rade_encoder",
            in_sig=[np.int16], out_sig=[np.complex64],
        )
        self._lock = threading.Lock()
        self._session = rade.RadeSession()
        self._lpcnet = LpcnetDemoProcess(MODE_FEATURES)

        self._n_pcm_per_frame = LPCNET_FRAME_SIZE * LPCNET_FRAMES_PER_RADE_FRAME  # 1920
        self._n_iq_per_frame = self._session.n_tx_out  # 960

        self.set_output_multiple(self._n_iq_per_frame)
        self.set_relative_rate(self._n_iq_per_frame, self._n_pcm_per_frame)

    def forecast(self, noutput_items, ninputs):
        # NOTE: gnuradio.gr.gateway's Python forecast() signature differs
        # from the C++ gr::block::forecast() -- it takes the input PORT
        # COUNT (an int) and must RETURN a list of required item counts.
        frames = max(1, noutput_items // self._n_iq_per_frame)
        return [frames * self._n_pcm_per_frame] * ninputs

    def general_work(self, input_items, output_items):
        in0 = input_items[0]
        out0 = output_items[0]
        frames = min(len(in0) // self._n_pcm_per_frame, len(out0) // self._n_iq_per_frame)
        if frames <= 0:
            self.consume_each(0)
            return 0

        with self._lock:
            for i in range(frames):
                pcm_chunk = in0[i * self._n_pcm_per_frame:(i + 1) * self._n_pcm_per_frame]
                feats = np.empty(self._session.n_features_in_out, dtype=np.float32)
                for j in range(LPCNET_FRAMES_PER_RADE_FRAME):
                    sub_pcm = pcm_chunk[j * LPCNET_FRAME_SIZE:(j + 1) * LPCNET_FRAME_SIZE]
                    feats[j * NB_TOTAL_FEATURES:(j + 1) * NB_TOTAL_FEATURES] = self._lpcnet.encode_frame(sub_pcm)
                out0[i * self._n_iq_per_frame:(i + 1) * self._n_iq_per_frame] = self._session.tx(feats)

        self.consume_each(frames * self._n_pcm_per_frame)
        return frames * self._n_iq_per_frame

    def send_eoo(self) -> np.ndarray:
        """One-shot End-of-Over IQ tail (rade_n_tx_eoo_out() samples) --
        called by the flowgraph on unkey when EOO is enabled, NOT part of
        the streaming work() path above."""
        with self._lock:
            return self._session.tx_eoo()

    def close(self):
        with self._lock:
            self._lpcnet.close()
            self._session.close()
