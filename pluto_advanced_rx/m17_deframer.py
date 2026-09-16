"""GNU Radio message-sink block that consumes gr-m17's m17_decoder "fields"
message port (a PMT dict with src/dst/type/meta[/sms] keys, emitted once per
fully-received Link Setup Frame -- see gr-m17/lib/m17_decoder_impl.cc) and
invokes a plain Python callback with the decoded fields as a native dict.

Same "gr.basic_block with a Python callback surface" idiom already
established by PSK31VaricodeDeframer/FileBroadcastDeframer, adapted for a
MESSAGE port input instead of a stream input -- m17_decoder has no
equivalent "decoded fields as a byte stream" output, only this message
port, so a gr.sync_block with in_sig=[...] doesn't apply here. This is the
first message-port consumer in this codebase (verified: no existing
pattern to follow byte-for-byte); the message_port_register_in()/
set_msg_handler() shape below was verified directly against a real,
working TX->RX M17 loopback this session (pmt.to_python() on a captured
"fields" message returns a plain dict: {'src': str, 'dst': str,
'type': numpy.uint8[2], 'meta': numpy.uint8[14]}, confirmed empirically,
not assumed from documentation)."""
import threading

import pmt
from gnuradio import gr


class M17FieldsDeframer(gr.basic_block):
    def __init__(self, on_fields):
        gr.basic_block.__init__(self, name="M17FieldsDeframer", in_sig=[], out_sig=[])
        self._on_fields = on_fields
        self._lock = threading.Lock()
        self._frames_decoded = 0
        self.message_port_register_in(pmt.intern("fields"))
        self.set_msg_handler(pmt.intern("fields"), self._handle)

    def _handle(self, msg):
        fields = pmt.to_python(msg)
        with self._lock:
            self._frames_decoded += 1
        # Called on the GNU Radio message-passing thread, NOT the Qt GUI
        # thread -- same cross-thread constraint FileBroadcastDeframer's/
        # PSK31VaricodeDeframer's own callbacks already operate under
        # (fed from the GR scheduler thread, read from a Qt timer poll);
        # on_fields() must itself be thread-safe, matching
        # on_filebroadcast_frame/on_psk31_char's existing contract.
        self._on_fields(fields)

    @property
    def frames_decoded(self):
        with self._lock:
            return self._frames_decoded
