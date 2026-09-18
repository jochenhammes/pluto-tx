"""GNU Radio message-sink block that consumes LoraRxDecoder's "msg" output
(one PMT symbol per CRC-verified LoRa frame) and hands the raw payload bytes
to a Python callback -- the Meshtastic counterpart of m17_deframer.py's
M17FieldsDeframer (same message-port idiom, see there).

The bytes are extracted with pluto_tx.lora.decode_pmt_symbol_bytes(), NOT
pmt.symbol_to_string(): real Meshtastic frames are encrypted binary, and the
UTF-8 path corrupts them (see pluto_tx/lora.py's 6th real bug). The callback
runs on the GNU Radio message thread, not the Qt thread -- it must be
thread-safe (see meshtastic_state.MeshtasticState)."""
import threading

import pmt
from gnuradio import gr

from pluto_tx.lora import decode_pmt_symbol_bytes


class MeshtasticDeframer(gr.basic_block):
    def __init__(self, on_frame):
        gr.basic_block.__init__(self, name="MeshtasticDeframer", in_sig=[], out_sig=[])
        self._on_frame = on_frame
        self._lock = threading.Lock()
        self._frames_decoded = 0
        self.message_port_register_in(pmt.intern("msg"))
        self.set_msg_handler(pmt.intern("msg"), self._handle)

    def _handle(self, msg):
        try:
            raw = decode_pmt_symbol_bytes(msg)
        except AssertionError:
            return  # not a PMT symbol -- ignore rather than kill the scheduler thread
        with self._lock:
            self._frames_decoded += 1
        self._on_frame(raw)

    @property
    def frames_decoded(self):
        with self._lock:
            return self._frames_decoded
