"""ADS-L through the real RX flowgraph (GNU Radio blocks, fake device replaying synthetic IQ)."""
import time
import unittest

import numpy as np

from pluto_advanced_rx import config
from tests import adsl_testgen as gen


def scene(rate, packets, noise_snr_db=None, offset_hz=0.0, lo_hz=config.ADSL_LO_HZ, seed=1, pad_s=0.05):
    """packets: list of (channel_hz, start_s, address). All bursts unit amplitude, on their real offset from the LO."""
    rng = np.random.default_rng(seed)
    total = int((max(p[1] for p in packets) + 0.02 + pad_s) * rate)
    x = np.zeros(total, np.complex128)
    for channel_hz, start_s, address in packets:
        burst = gen.modulate(gen.air_chips(gen.frame(gen.traffic_clear(address=address))), rate)
        start = int((start_s + pad_s) * rate)
        t = np.arange(start, start + len(burst))
        x[start:start + len(burst)] += burst * np.exp(2j * np.pi * (channel_hz - lo_hz + offset_hz) * t / rate)
    if noise_snr_db is not None:
        sigma = np.sqrt(10 ** (-noise_snr_db / 10) * rate / 200_000 / 2)
        x += sigma * (rng.standard_normal(total) + 1j * rng.standard_normal(total))
    return x.astype(np.complex64)


def run(iq, rate, lo_hz=config.ADSL_LO_HZ, **kw):
    from pluto_advanced_rx import flowgraph as rxf
    from tests import fakes
    fakes.make_fake_rx(iq, rate)
    out = []
    rx = rxf.AdvancedRxFlowgraph(uri="x", frequency=lo_hz, sample_rate=rate, device_type="fake",
                                 active_digimode="adsl", on_adsl_packet=out.append, **kw)
    rx.start()
    rx.wait()
    for _channel, _xlate, _demod, deframer in rx.adsl_channels:
        deframer._detector.flush()
    return out, rx


class FlowgraphTests(unittest.TestCase):
    def test_both_channels_at_every_device_rate(self):
        for rate in (2_000_000, 2_048_000, 2_400_000, 2_500_000, 4_000_000):
            with self.subTest(rate=rate):
                iq = scene(rate, [(868_200_000, 0.0, 0x111111), (868_400_000, 0.05, 0x222222)], noise_snr_db=25)
                out, _rx = run(iq, rate)
                self.assertEqual({(p["channel_hz"], p["address"]) for p in out},
                                 {(868_200_000, 0x111111), (868_400_000, 0x222222)})

    def test_simultaneous_packets_on_both_channels(self):
        iq = scene(2_400_000, [(868_200_000, 0.0, 0x111111), (868_400_000, 0.0, 0x222222)], noise_snr_db=25)
        out, _rx = run(iq, 2_400_000)
        self.assertEqual({(p["channel_hz"], p["address"]) for p in out},
                         {(868_200_000, 0x111111), (868_400_000, 0x222222)})

    def test_frequency_error_is_reported(self):
        iq = scene(2_400_000, [(868_200_000, 0.0, 0x111111)], noise_snr_db=25, offset_hz=30_000)
        out, _rx = run(iq, 2_400_000)
        self.assertEqual(len(out), 1)
        self.assertAlmostEqual(out[0]["frequency_offset_hz"], 30_000, delta=3000)

    def test_sensitivity_through_gnu_radio_blocks(self):
        rate = 2_400_000
        packets = [(config.ADSL_CHANNELS_HZ[i % 2], 0.01 * i, 0x100000 + i) for i in range(60)]
        out, _rx = run(scene(rate, packets, noise_snr_db=12), rate)
        self.assertGreaterEqual(len({p["address"] for p in out}), 58)

    def test_retune_follows_the_channels(self):
        rate = 2_400_000
        iq = scene(rate, [(868_200_000, 0.0, 0x111111)], noise_snr_db=25, lo_hz=868_860_000)
        from pluto_advanced_rx import flowgraph as rxf
        from tests import fakes
        fakes.make_fake_rx(iq, rate)
        out = []
        rx = rxf.AdvancedRxFlowgraph(uri="x", frequency=config.ADSL_LO_HZ, sample_rate=rate, device_type="fake",
                                     active_digimode="adsl", on_adsl_packet=out.append)
        rx.set_frequency(868_860_000)
        rx.start()
        rx.wait()
        for _channel, _xlate, _demod, deframer in rx.adsl_channels:
            deframer._detector.flush()
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["channel_hz"], 868_200_000)
        self.assertLess(abs(out[0]["frequency_offset_hz"]), 3000)

    def test_too_low_sample_rate_is_refused(self):
        from pluto_advanced_rx import flowgraph as rxf
        from tests import fakes
        fakes.make_fake_rx(np.zeros(100, np.complex64), 1_000_000)
        with self.assertRaisesRegex(ValueError, "too low for ADS-L"):
            rxf.AdvancedRxFlowgraph(uri="x", frequency=config.ADSL_LO_HZ, sample_rate=1_000_000, device_type="fake",
                                    active_digimode="adsl")

    def test_other_digimodes_do_not_wire_adsl(self):
        from pluto_advanced_rx import flowgraph as rxf
        from tests import fakes
        fakes.make_fake_rx(np.zeros(100, np.complex64), 2_500_000)
        rx = rxf.AdvancedRxFlowgraph(uri="x", frequency=433e6, sample_rate=2_500_000, device_type="fake")
        self.assertEqual(rx.adsl_channels, [])


if __name__ == "__main__":
    unittest.main()
