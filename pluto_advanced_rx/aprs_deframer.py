"""GNU Radio sink block for APRS: demodulated audio in, AX.25 frames out through a callback. All signal logic lives in
aprs_demod.py and all protocol logic in pluto_tx/aprs_codec.py; this is only the GNU Radio wrapper."""
import numpy as np
from gnuradio import gr

from .aprs_demod import AprsFrameDetector


class AprsDeframer(gr.sync_block):
    def __init__(self, on_frame, sample_rate):
        gr.sync_block.__init__(self, name="AprsDeframer", in_sig=[np.float32], out_sig=None)
        self.detector = AprsFrameDetector(sample_rate, on_frame)

    def work(self, input_items, output_items):
        self.detector.feed(input_items[0])
        return len(input_items[0])
