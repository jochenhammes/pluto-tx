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


if __name__ == "__main__":
    unittest.main()
