"""GNU Radio sink block for one ADS-L channel: FM discriminator samples in, packets out through a callback. All signal
logic lives in adsl_demod.py and all protocol logic in pluto_tx/adsl_codec.py; this is only the GNU Radio wrapper. One
instance per channel, so 868.2 and 868.4 MHz are received at the same time."""
import numpy as np
from gnuradio import gr

from .adsl_demod import AdslBurstDetector


class AdslDeframer(gr.sync_block):
    def __init__(self, on_packet, channel_hz, sample_rate):
        gr.sync_block.__init__(self, name=f"AdslDeframer{int(channel_hz)}", in_sig=[np.float32], out_sig=None)
        self.channel_hz = int(channel_hz)
        self._on_packet = on_packet
        self._detector = AdslBurstDetector(sample_rate, self._deliver)

    def _deliver(self, info):
        info["channel_hz"] = self.channel_hz
        self._on_packet(info)

    def work(self, input_items, output_items):
        self._detector.feed(input_items[0])
        return len(input_items[0])

    @property
    def packets(self):
        return self._detector.frames_ok

    @property
    def crc_failures(self):
        return self._detector.frames_bad
