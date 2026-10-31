"""FftProbe.get_rows_since(): every row since a generation, not only the latest -- Web-TRX sends all
of them so its waterfall doesn't drop rows (it polls slower than rows are made)."""
import unittest

import numpy as np
from gnuradio import blocks, gr

from pluto_advanced_rx import config
from pluto_advanced_rx.fft_probe import FftProbe


def run_probe(n_samples, rate=240_000):
    probe = FftProbe(256, rate, config.WATERFALL_WINDOW, 30)
    tb = gr.top_block()
    rng = np.random.default_rng(0)
    src = blocks.vector_source_c((rng.normal(size=n_samples) + 1j * rng.normal(size=n_samples)).astype(np.complex64))
    tb.connect(src, probe)
    tb.run()
    return probe


class RowsSinceTests(unittest.TestCase):
    def test_returns_all_recent_rows_in_order_and_nothing_twice(self):
        probe = run_probe(240_000)                            # 1 s at 30 rows/s
        rows, gen = probe.get_rows_since(-1)
        self.assertGreaterEqual(gen, 25)
        self.assertEqual(len(rows), min(gen, FftProbe.RECENT_ROWS))
        self.assertTrue(all(r.shape == (256,) for r in rows))
        latest, _ = probe.get_latest_row()
        np.testing.assert_array_equal(rows[-1], latest)       # newest last
        again, gen2 = probe.get_rows_since(gen)
        self.assertEqual((again, gen2), ([], gen))            # nothing new -> nothing returned
        three, _ = probe.get_rows_since(gen - 3)
        self.assertEqual(len(three), 3)

    def test_reconfigure_drops_rows_of_the_old_size(self):
        probe = run_probe(80_000)
        probe.set_fft_size(512)
        self.assertEqual(probe.get_rows_since(-1)[0], [])


class ViewOffsetTests(unittest.TestCase):
    RATE = 2_400_000
    SIZE = 1024

    def peak_hz(self, tone_offset_hz, zoom, view_offset_hz):
        """Where a tone appears in the rows, relative to the tuned frequency."""
        probe = FftProbe(self.SIZE, self.RATE, config.WATERFALL_WINDOW, 30)
        probe.set_zoom(zoom)
        probe.set_view_offset(view_offset_hz)
        n = self.RATE // 2
        tone = np.exp(2j * np.pi * tone_offset_hz * np.arange(n) / self.RATE).astype(np.complex64)
        tb = gr.top_block()
        tb.connect(blocks.vector_source_c(tone.tolist(), False), probe)
        tb.run()
        row, _ = probe.get_latest_row()
        span = self.RATE / probe.zoom
        return (int(np.argmax(row)) - self.SIZE // 2) * span / self.SIZE + probe.view_offset_hz, probe

    def test_a_tone_stays_at_its_true_frequency(self):
        for tone in (-650_000, -450_000):
            found, probe = self.peak_hz(tone, 4, -550_000)
            self.assertAlmostEqual(probe.view_offset_hz, -550_000, delta=self.RATE / 4 / self.SIZE)
            self.assertAlmostEqual(found, tone, delta=self.RATE / 4 / self.SIZE * 2)

    def test_offset_is_limited_to_the_spectrum(self):
        _found, probe = self.peak_hz(0, 2, -1_000_000)
        self.assertEqual(probe.view_offset_hz, -600_000)  # half the rate minus half the shown span

    def test_no_offset_at_zoom_1(self):
        _found, probe = self.peak_hz(0, 1, -550_000)
        self.assertEqual(probe.view_offset_hz, 0)

    def test_setting_zoom_keeps_the_offset(self):
        probe = FftProbe(self.SIZE, self.RATE, config.WATERFALL_WINDOW, 30)
        probe.set_view_offset(-550_000)
        self.assertEqual(probe.view_offset_hz, 0)
        probe.set_zoom(4)
        self.assertAlmostEqual(probe.view_offset_hz, -550_000, delta=self.RATE / 4 / self.SIZE)


if __name__ == "__main__":
    unittest.main()
