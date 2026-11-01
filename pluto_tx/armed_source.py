"""One one-shot transmission's audio, held back until arm(): silence until then, the audio, then the end of the stream.
It keeps the modulator fed while the transmitter comes up, and starting it takes no flowgraph lock: swapping a source
in after keying left a Pluto on bare carrier for seconds (measured over the air with APRS; see docs/aprs/SPEC.md).
Downstream back-pressure paces it, so it is only installed while keying (PlutoTxFlowgraph._install_armed_source)."""
import threading

import numpy as np
from gnuradio import gr


class ArmedAudioSource(gr.sync_block):
    def __init__(self, audio):
        gr.sync_block.__init__(self, name="armed_audio_source", in_sig=None, out_sig=[np.float32])
        self._audio = np.asarray(audio, dtype=np.float32)
        self._position = 0
        self._armed = False
        self._stopped = False
        self._lock = threading.Lock()

    def arm(self):
        with self._lock:
            self._armed = True

    def stop_playing(self):
        """Ends the stream, also if it was never armed."""
        with self._lock:
            self._stopped = True

    def work(self, input_items, output_items):
        out = output_items[0]
        with self._lock:
            if self._stopped:
                return -1
            if not self._armed:
                out[:] = 0.0
                return len(out)
            n = min(len(out), len(self._audio) - self._position)
            if n <= 0:
                return -1
            out[:n] = self._audio[self._position:self._position + n]
            self._position += n
            return n
