"""GNU Radio sink block for ADS-B: signal magnitude in, messages out through a callback. All signal logic lives in
adsb_demod.py and all protocol logic in pluto_tx/adsb_codec.py; this is only the GNU Radio wrapper."""
import numpy as np
from gnuradio import gr

from .adsb_demod import AdsbFrameDetector


class AdsbDeframer(gr.sync_block):
    def __init__(self, on_message, sample_rate):
        gr.sync_block.__init__(self, name="AdsbDeframer", in_sig=[np.float32], out_sig=None)
        self.detector = AdsbFrameDetector(sample_rate, on_message)

    def work(self, input_items, output_items):
        self.detector.feed(input_items[0])
        return len(input_items[0])
