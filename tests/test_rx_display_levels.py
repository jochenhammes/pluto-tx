"""RX app spectrum/waterfall levels: averaging default and the noise-floor auto-level."""
import unittest

import numpy as np

from pluto_advanced_rx import config


class AutoLevelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PyQt5 import QtWidgets
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def window(self):
        from pluto_advanced_rx import gui
        w = gui.MainWindow("x")
        self.addCleanup(w.close)
        return w

    def feed(self, w, noise_db, peak_db=None):
        w._start_autolevel()
        w._autolevel_after = 0.0                       # skip the AGC settle time
        rng = np.random.default_rng(0)
        for _ in range(config.WATERFALL_AUTOLEVEL_ROWS):
            row = noise_db + rng.normal(0, 3, 1024)
            if peak_db is not None:
                row[500:520] = peak_db                 # a strong signal must not lift the noise estimate
            w._autolevel_feed(row.astype(np.float32))

    def test_defaults(self):
        w = self.window()
        self.assertEqual(w.avg_slider.value(), 3)
        self.assertEqual((w.db_floor_slider.value(), w.db_ceiling_slider.value()),
                         tuple(int(v) for v in config.WATERFALL_DB_RANGE))

    def test_levels_follow_the_noise_floor_in_both_directions(self):
        w = self.window()
        for noise, expected in ((-16.0, (-31, 29)), (-90.0, (-105, -45)), (-39.0, (-54, 6))):
            with self.subTest(noise=noise):
                self.feed(w, noise, peak_db=noise + 60)
                self.assertEqual((w.db_floor_slider.value(), w.db_ceiling_slider.value()), expected)
                self.assertEqual(w.waterfall._db_range, tuple(float(v) for v in expected))
                self.assertIsNone(w._autolevel_rows)    # one-shot


if __name__ == "__main__":
    unittest.main()
