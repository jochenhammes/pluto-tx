"""GNU Radio sink block that decodes POCSAG paging from a sliced bit stream (1 bit per byte, one
symbol per sample, the output of digital.binary_slicer_fb() behind a symbol_sync_ff -- see the POCSAG
branch in flowgraph.py). All protocol logic lives in pluto_tx/pocsag_codec.py (BatchDecoder: sync
search in both polarities, batch following, BCH correction, message assembly); this block is only the
GNU Radio wrapper plus counters for the GUI. One instance per baud rate, so 512/1200/2400 are decoded
in parallel without any mode switch."""
import threading

import numpy as np
from gnuradio import gr

from pluto_tx.pocsag_codec import BatchDecoder


class PocsagDeframer(gr.sync_block):
    def __init__(self, on_message, baud):
        gr.sync_block.__init__(self, name=f"PocsagDeframer{baud}", in_sig=[np.uint8], out_sig=None)
        self.baud = int(baud)
        self._on_message = on_message
        self._lock = threading.Lock()
        self._decoder = BatchDecoder(self._deliver)

    def _deliver(self, msg):
        msg["baud"] = self.baud
        self._on_message(msg)

    def work(self, input_items, output_items):
        bits = input_items[0]
        with self._lock:
            push = self._decoder.push_bit
            for b in bits:
                push(int(b))
        return len(bits)

    @property
    def batches(self):
        return self._decoder.batches

    @property
    def codewords_ok(self):
        return self._decoder.codewords_ok

    @property
    def codewords_bad(self):
        return self._decoder.codewords_bad

    @property
    def last_lock_time(self):
        return self._decoder.last_lock_time
