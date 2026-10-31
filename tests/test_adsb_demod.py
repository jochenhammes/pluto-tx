"""ADS-B demodulator on synthetic pulses (no GNU Radio). Nothing here ever reaches a transmitter."""
import unittest

import numpy as np

from pluto_advanced_rx.adsb_demod import AdsbFrameDetector
from pluto_tx import adsb_codec as c
from tests import adsb_measure as m
from tests import adsb_testgen as gen

RATE = 2_400_000
MESSAGE = gen.position_message(0x3C6444, 53.5, 9.5, 35000, 0)


def burst(message=MESSAGE, rate=RATE, snr_db=30, delay_us=0.37, seed=1, **kw):
    rng = np.random.default_rng(seed)
    x = np.concatenate([np.zeros(int(800 * rate / 1e6)), gen.iq(message, rate, delay_us=delay_us, **kw), np.zeros(int(800 * rate / 1e6))])  # a burst must not dominate the noise estimate
    return m.channel(x, rng, snr_db)


class DemodTests(unittest.TestCase):
    def test_message_content(self):
        got, _ = m.run(burst(), RATE)
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]["icao"], "3C6444")
        self.assertEqual(got[0]["alt_ft"], 35000)
        self.assertFalse(got[0]["corrected"])
        self.assertGreater(got[0]["snr_db"], 10)

    def test_every_usable_rate(self):
        for rate in (2_400_000, 2_500_000, 2_560_000, 3_200_000, 4_000_000, 5_000_000, 8_000_000, 10_000_000):
            with self.subTest(rate=rate):
                self.assertEqual(len(m.run(burst(rate=rate), rate)[0]), 1)

    def test_any_timing_against_the_sample_clock(self):
        for delay in np.linspace(0, 1, 11):
            with self.subTest(delay=delay):
                self.assertEqual(len(m.run(burst(delay_us=float(delay)), RATE)[0]), 1)

    def test_block_boundaries_lose_nothing(self):
        iq, icaos = m.stream(RATE, 60, np.random.default_rng(2), snr_db=30)
        for chunk in (100, 4096, 70000):
            with self.subTest(chunk=chunk):
                got, _ = m.run(iq, RATE, chunk=chunk)
                self.assertEqual(sorted(int(g["icao"], 16) for g in got), sorted(icaos))

    def test_frequency_offset_does_not_matter(self):
        for offset in (-500e3, 300e3):
            self.assertEqual(len(m.run(burst(freq_offset_hz=offset), RATE)[0]), 1)

    def test_noise_makes_no_messages(self):
        rng = np.random.default_rng(5)
        n = RATE * 2
        noise = rng.standard_normal(n) + 1j * rng.standard_normal(n)
        got, _ = m.run(noise, RATE)
        self.assertEqual(got, [])

    def test_sensitivity_floor(self):
        """Measured (adsb_measure): 100 % at 20 dB, 79 % at 15 dB at 2.4 MS/s with random pulse timing."""
        self.assertGreaterEqual(m.quote(RATE, 20, n=80)[0], 0.97)
        self.assertGreaterEqual(m.quote(4_000_000, 15, n=80)[0], 0.97)

    def flipped(self, bit):
        bad = bytearray(MESSAGE)
        bad[bit // 8] ^= 0x80 >> bit % 8
        return bytes(bad)

    def test_a_flipped_bit_of_an_unknown_address_is_refused(self):
        got, det = m.run(burst(self.flipped(60)), RATE)
        self.assertEqual(got, [])
        self.assertEqual(det.corrected, 0)

    def test_a_flipped_bit_of_a_known_address_is_corrected(self):
        x = np.concatenate([burst(MESSAGE), burst(self.flipped(60))])
        got, det = m.run(x, RATE)
        self.assertEqual(len(got), 2)
        self.assertTrue(got[1]["corrected"])
        self.assertEqual(got[1]["alt_ft"], 35000)
        self.assertEqual(det.corrected, 1)

    def test_two_flipped_bits_are_refused(self):
        bad = bytearray(self.flipped(60))
        bad[10] ^= 0x10
        got, _ = m.run(np.concatenate([burst(MESSAGE), burst(bytes(bad))]), RATE)
        self.assertEqual(len(got), 1)

    def test_other_formats_are_ignored(self):
        got, _ = m.run(burst(gen.encode_message(0x111111, 11, 0, df=11)), RATE)
        self.assertEqual(got, [])

    def test_df_field_never_gets_corrected(self):
        self.assertEqual(c.downlink_format(MESSAGE), 17)
        got, _ = m.run(np.concatenate([burst(MESSAGE), burst(self.flipped(2))]), RATE)
        self.assertEqual(len(got), 1)


class ThresholdTests(unittest.TestCase):
    def test_rate_limit_is_exposed(self):
        self.assertEqual(AdsbFrameDetector(RATE, print).samples_per_us, 2.4)


if __name__ == "__main__":
    unittest.main()
