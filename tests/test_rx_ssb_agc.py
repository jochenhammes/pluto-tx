"""RX SSB/LSB audio AGC: a weak sideband signal comes out at a normal audio level (before, the
demodulated audio carried the raw IF level, ~70 dB too quiet on an RTL-SDR), and a strong one is not
driven into clipping."""
import time
import unittest

import numpy as np
from gnuradio import blocks

RATE = 1_000_000


def rx_audio(demod_mode, tone_hz, amplitude):
    from pluto_advanced_rx import flowgraph as rxf
    from tests import fakes
    t = np.arange(RATE * 3) / RATE
    rng = np.random.default_rng(0)
    iq = amplitude * np.exp(2j * np.pi * tone_hz * t) + 1e-5 * (rng.normal(size=len(t)) + 1j * rng.normal(size=len(t)))
    fakes.make_fake_rx(iq.astype(np.complex64), RATE)
    rx = rxf.AdvancedRxFlowgraph(uri="x", frequency=14_200_000.0, sample_rate=RATE, device_type="fake",
                                 demod_mode=demod_mode)
    sink = blocks.vector_sink_f()
    rx.connect(rx.demod_selector, sink)
    rx.start()
    time.sleep(2.5)
    rx.stop()
    rx.wait()
    audio = np.array(sink.data(), dtype=np.float32)
    return audio[len(audio) // 2:]                                # after the AGC settled


class SsbAgcTests(unittest.TestCase):
    def test_weak_and_strong_usb_signals_come_out_at_a_similar_normal_level(self):
        from pluto_advanced_rx import flowgraph as rxf
        weak = rx_audio(rxf.AdvancedRxFlowgraph.MODE_SSB, 1000.0, 3e-4)    # ~-70 dBFS at the IF
        strong = rx_audio(rxf.AdvancedRxFlowgraph.MODE_SSB, 1000.0, 0.3)
        rms_weak, rms_strong = (float(np.sqrt(np.mean(a ** 2))) for a in (weak, strong))
        self.assertGreater(rms_weak, 0.1)                        # audible, not ~3e-4
        self.assertLess(abs(20 * np.log10(rms_weak / rms_strong)), 3.0)
        self.assertLess(float(np.abs(strong).max()), 1.0)       # no clipping

    def test_lsb_still_rejects_the_upper_sideband(self):
        from pluto_advanced_rx import flowgraph as rxf
        usb_in_lsb = rx_audio(rxf.AdvancedRxFlowgraph.MODE_LSB, 1000.0, 3e-4)   # only the AGC-amplified noise
        lsb_in_lsb = rx_audio(rxf.AdvancedRxFlowgraph.MODE_LSB, -1000.0, 3e-4)
        spec = lambda a: np.abs(np.fft.rfft(a * np.hanning(len(a))))
        f = np.fft.rfftfreq(len(usb_in_lsb), 1 / 48000)
        k = np.argmin(np.abs(f - 1000))
        self.assertGreater(spec(lsb_in_lsb)[k - 2:k + 3].max(), 30 * spec(usb_in_lsb)[k - 2:k + 3].max())


if __name__ == "__main__":
    unittest.main()
