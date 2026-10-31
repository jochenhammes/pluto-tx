"""ADS-L burst demodulator on synthetic bursts through a numpy copy of the channel path (no GNU Radio)."""
import unittest

import numpy as np

from pluto_advanced_rx.adsl_demod import AdslBurstDetector, DuplicateFilter
from pluto_tx import adsl_codec as c
from tests import adsl_measure as m
from tests import adsl_testgen as gen


def detect(iq, rate, chunk=None, seed=0):
    disc, out_rate = m.channelize(iq, rate)
    got = []
    detector = AdslBurstDetector(out_rate, got.append)
    if chunk is None:
        detector.feed(disc)
    else:
        rng = np.random.default_rng(seed)
        i = 0
        while i < len(disc):
            n = int(rng.integers(1, chunk))
            detector.feed(disc[i:i + n])
            i += n
    detector.flush()
    return got, detector


def one_burst(rate, frame_bytes=None, preamble_repeats=8, invert=False, offset_hz=0.0):
    frame_bytes = frame_bytes or gen.frame(gen.traffic_clear())
    pad = np.zeros(int(0.002 * rate), np.complex64)
    iq = np.concatenate([pad, gen.modulate(gen.air_chips(frame_bytes, preamble_repeats), rate, invert=invert), pad])
    return iq * np.exp(2j * np.pi * offset_hz * np.arange(len(iq)) / rate)


class DemodTests(unittest.TestCase):
    def test_content_survives_the_radio_path(self):
        got, _ = detect(one_burst(2_400_000), 2_400_000)
        self.assertEqual(len(got), 1)
        self.assertEqual(c.address_label(got[0]), "FANET:123456")
        self.assertEqual(got[0]["alt_m"], 1234)
        self.assertAlmostEqual(got[0]["lat"], 48.12345, places=4)

    def test_every_device_rate(self):
        for rate in (1_800_000, 1_920_000, 2_000_000, 2_048_000, 2_400_000, 2_500_000, 3_200_000, 4_000_000, 5_000_000):
            with self.subTest(rate=rate):
                self.assertEqual(len(detect(one_burst(rate), rate)[0]), 1)

    def test_preamble_lengths_allowed_by_the_specification(self):
        for repeats in (5, 8, 12):
            with self.subTest(repeats=repeats):
                self.assertEqual(len(detect(one_burst(2_400_000, preamble_repeats=repeats), 2_400_000)[0]), 1)

    def test_inverted_spectrum_is_flagged(self):
        got, _ = detect(one_burst(2_400_000, invert=True), 2_400_000)
        self.assertEqual(len(got), 1)
        self.assertTrue(got[0]["inverted"])

    def test_frequency_error_is_tolerated_and_reported(self):
        for offset in (-45_000, -20_000, 20_000, 45_000):
            with self.subTest(offset=offset):
                got, _ = detect(one_burst(2_400_000, offset_hz=offset), 2_400_000)
                self.assertEqual(len(got), 1)
                self.assertAlmostEqual(got[0]["frequency_offset_hz"], offset, delta=3000)

    def test_longer_frame_uses_the_length_byte(self):
        clear = bytes((6,)) + gen.traffic_clear()[1:] + bytes(16)
        got, _ = detect(one_burst(2_400_000, gen.frame(clear)), 2_400_000)
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]["kind"], "remote-id")

    def test_arbitrary_block_boundaries_lose_nothing(self):
        rate = 2_400_000
        iq, sent = m.burst_iq(rate, np.random.default_rng(2), 30, 0, 40)
        for chunk in (50, 5000, 40000):
            with self.subTest(chunk=chunk):
                got, _ = detect(iq, rate, chunk=chunk, seed=chunk)
                self.assertEqual(sorted(p["address"] for p in got), sorted(sent))

    def test_bursts_right_behind_each_other(self):
        rate = 2_400_000
        bursts = [gen.modulate(gen.air_chips(gen.frame(gen.traffic_clear(address=a))), rate) for a in (1, 2, 3)]
        got, _ = detect(np.concatenate(bursts + [np.zeros(20000, np.complex64)]), rate)
        self.assertEqual([p["address"] for p in got], [1, 2, 3])

    def test_noise_yields_no_packets(self):
        rng = np.random.default_rng(4)
        n = 2_400_000
        got, detector = detect(rng.standard_normal(n) + 1j * rng.standard_normal(n), 2_400_000)
        self.assertEqual(got, [])
        self.assertEqual(detector.frames_ok, 0)

    def test_sensitivity_floor(self):
        """Measured floor: 100 % at 10 dB in the 200 kHz channel with the 110 kHz filter (see adsl_measure)."""
        self.assertGreaterEqual(m.received(2_400_000, 12, n_packets=50)[0], 0.98)

    def test_corrupted_frame_is_not_delivered(self):
        frame = bytearray(gen.frame(gen.traffic_clear()))
        frame[10] ^= 0x10
        got, detector = detect(one_burst(2_400_000, bytes(frame)), 2_400_000)
        self.assertEqual(got, [])
        self.assertEqual(detector.frames_bad, 1)


class DuplicateFilterTests(unittest.TestCase):
    def test_same_frame_once_within_the_window(self):
        got = []
        f = DuplicateFilter(got.append)
        f({"raw": "aa", "channel_hz": 1}, now=10.0)
        f({"raw": "aa", "channel_hz": 2}, now=10.1)
        f({"raw": "bb", "channel_hz": 2}, now=10.1)
        self.assertEqual([(p["raw"], p["channel_hz"]) for p in got], [("aa", 1), ("bb", 2)])
        self.assertEqual(f.duplicates, 1)

    def test_a_later_identical_frame_is_a_new_packet(self):
        got = []
        f = DuplicateFilter(got.append)
        f({"raw": "aa"}, now=10.0)
        f({"raw": "aa"}, now=11.0)
        self.assertEqual(len(got), 2)


class NeighbourTests(unittest.TestCase):
    def test_strong_neighbour_is_not_reported_here(self):
        for snr in (40, 60):
            with self.subTest(snr=snr):
                iq, _ = m.burst_iq(2_400_000, np.random.default_rng(5), snr, 200_000, 20)
                self.assertEqual(detect(iq, 2_400_000)[0], [])


if __name__ == "__main__":
    unittest.main()
