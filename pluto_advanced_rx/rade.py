"""RadeDecoder: a custom gr.basic_block wrapping the two-stage RADE V1 RX
pipeline (librade.so's rade_rx() IQ->features, then lpcnet_demo
features->speech) verified end-to-end offline this session (Phase F/H of
the RADE integration plan): complex64 IQ in (8kHz) -> int16 speech out
(16kHz).

Unlike RadeEncoder (pluto_tx/rade.py) this block has NO fixed
relative_rate: rade_nin(), the exact IQ sample count the NEXT rade_rx()
call needs, is NOT constant -- it varies with acquisition state (confirmed
on real data this session: nin_max()=1120 while unsynced/searching,
narrows once locked) -- rade_api.h's own documented contract, re-queried
fresh via forecast() on every call rather than assumed fixed. Consequently
this processes at most ONE rade_rx() call per general_work() invocation
(GNU Radio's scheduler simply calls back again immediately if more input
is available, which is normal/expected for a variable-rate block).

A second, independent source of irregular output cadence: lpcnet_demo's
"-fargan-synthesis" mode consumes its first 5 feature frames as FARGAN
warm-up context with NO PCM output at all (see lpcnet_subprocess.py's
module docstring) -- so even a single successful, in-sync rade_rx() call
does not necessarily yield PCM output on the frame it was decoded from.
_pcm_queue below absorbs this mismatch between "IQ consumed this call" and
"PCM produced this call", something a fixed relative_rate could not
express -- exactly the kind of block gr.basic_block (as opposed to
gr.sync_block) exists for.

Exposes synced/freq_offset_hz/snr_db (updated every call rade_rx()
actually ran) for the GUI's status display.
"""
import threading

import numpy as np
from gnuradio import gr

from . import rade_ctypes as rade
from .lpcnet_subprocess import LpcnetDemoProcess, MODE_FARGAN_SYNTHESIS, NB_TOTAL_FEATURES

LPCNET_FRAMES_PER_RADE_FRAME = 12  # rade_n_features_in_out() (432) / NB_TOTAL_FEATURES (36)


class RadeDecoder(gr.basic_block):
    def __init__(self):
        gr.basic_block.__init__(
            self, name="rade_decoder",
            in_sig=[np.complex64], out_sig=[np.int16],
        )
        self._lock = threading.Lock()
        self._session = rade.RadeSession()
        self._lpcnet = LpcnetDemoProcess(MODE_FARGAN_SYNTHESIS)
        self._pcm_queue = np.empty(0, dtype=np.int16)  # decoded audio not yet emitted downstream

        self.synced = False
        self.freq_offset_hz = 0.0
        self.snr_db = 0.0
        self.last_eoo_bits = None  # set whenever rade_rx() reports has_eoo -- EOO decoding itself is Phase I2

    def forecast(self, noutput_items, ninputs):
        return [self._session.nin()] * ninputs

    def general_work(self, input_items, output_items):
        in0 = input_items[0]
        out0 = output_items[0]

        with self._lock:
            needed = self._session.nin()
            if len(in0) < needed:
                self.consume_each(0)
                return 0

            features_out, has_eoo, eoo_bits = self._session.rx(in0[:needed])
            self.consume_each(needed)

            self.synced = self._session.sync()
            if self.synced:
                self.freq_offset_hz = self._session.freq_offset()
                self.snr_db = self._session.snr_db()
            if has_eoo:
                self.last_eoo_bits = eoo_bits

            if features_out is not None:
                for j in range(LPCNET_FRAMES_PER_RADE_FRAME):
                    sub = features_out[j * NB_TOTAL_FEATURES:(j + 1) * NB_TOTAL_FEATURES]
                    pcm_frame = self._lpcnet.synthesize_frame(sub)
                    if pcm_frame is not None:
                        self._pcm_queue = np.concatenate([self._pcm_queue, pcm_frame])

            n_out = min(len(out0), len(self._pcm_queue))
            if n_out > 0:
                out0[:n_out] = self._pcm_queue[:n_out]
                self._pcm_queue = self._pcm_queue[n_out:]
            return n_out

    def close(self):
        with self._lock:
            self._lpcnet.close()
            self._session.close()
