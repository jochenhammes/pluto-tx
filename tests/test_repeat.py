"""Repeat series for the one-shot digimodes: GUI (fake device, real Qt timers) and CLI argument handling."""
import time
import unittest

import numpy as np

from pluto_tx import flowgraph as txf

POCSAG = txf.PlutoTxFlowgraph.MODE_POCSAG


class GuiRepeatTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PyQt5 import QtWidgets
        from tests import fakes
        fakes.register_tx()
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def pump(self, seconds):
        end = time.time() + seconds
        while time.time() < end:
            self.app.processEvents()
            time.sleep(0.01)

    def window(self):
        from pluto_tx import gui
        w = gui.MainWindow("x")
        w.device_type_combo.addItem("fake", "fake")
        w.device_type_combo.setCurrentIndex(w.device_type_combo.findData("fake"))
        w.digimode_combo.setCurrentIndex(w.digimode_combo.findData(POCSAG))
        w.mode_tab_widget.setCurrentIndex(1)
        w._rebuild("x", None)
        self.addCleanup(lambda: w.tb is not None and w.tb.shutdown_safe())
        self.assertIsNotNone(w.tb)
        self.assertEqual(w.tb.mode, POCSAG)
        w.pocsag_text_edit.setText("R")
        w.pocsag_baud_combo.setCurrentIndex(w.pocsag_baud_combo.findData(2400))   # ~0.7 s per call
        return w

    def calls_on_air(self, w):
        """The fake sink only records while a signal flows, so on-air time / call duration = number of calls."""
        iq = np.array(w.tb.device.sink.data(), dtype=np.complex64)
        return round(float((np.abs(iq) > 0.01).sum()) / 2_500_000 / w.tb.pocsag_duration_s, 1)

    def test_series_runs_to_the_end_and_unkeys(self):
        w = self.window()
        w.repeat_count_spin.setValue(3)
        w.repeat_interval_spin.setValue(1.0)
        w.ptt_button.setChecked(True)
        self.assertTrue(w._repeat_active)
        self.assertFalse(w.repeat_count_spin.isEnabled())
        self.pump(1.4)
        self.assertIn("next in", w.ptt_button.text())
        self.assertFalse(w.tb.keyed)                                            # transmitter is off in the pause
        self.pump(6.0)
        self.assertFalse(w._repeat_active)
        self.assertFalse(w.tb.keyed)
        self.assertFalse(w.ptt_button.isChecked())
        self.assertIn("finished (3 sent)", w.status_label.text())
        self.assertTrue(w.repeat_count_spin.isEnabled())
        self.assertAlmostEqual(self.calls_on_air(w), 3.0, delta=0.4)

    def test_click_during_pause_ends_the_series(self):
        w = self.window()
        w.repeat_count_spin.setValue(5)
        w.repeat_interval_spin.setValue(2.0)
        w.ptt_button.setChecked(True)
        self.pump(1.5)                                                          # first call done, in the pause
        self.assertFalse(w.tb.keyed)
        w.ptt_button.setChecked(False)                                          # the second click
        self.assertFalse(w._repeat_active)
        self.pump(3.5)                                                          # the stale timer must not fire
        self.assertAlmostEqual(self.calls_on_air(w), 1.0, delta=0.2)
        self.assertFalse(w.tb.keyed)
        self.assertIn("stopped", w.status_label.text())
        self.assertEqual(w.ptt_button.text(), "PTT (click to send)")

    def test_click_during_transmission_ends_it_immediately(self):
        w = self.window()
        w.pocsag_baud_combo.setCurrentIndex(w.pocsag_baud_combo.findData(512))  # ~3 s per call
        w.repeat_count_spin.setValue(3)
        w.ptt_button.setChecked(True)
        self.pump(0.8)
        self.assertTrue(w.tb.keyed)
        w.ptt_button.setChecked(False)
        self.assertFalse(w.tb.keyed)
        self.pump(2.0)
        self.assertLess(self.calls_on_air(w), 1.4)                              # the fake sink is not real-time: only check no further calls

    def test_single_send_unchanged_and_refusal_stops_series(self):
        w = self.window()
        w.ptt_button.setChecked(True)                                           # count 1: no series
        self.assertFalse(w._repeat_active)
        self.pump(2.0)
        self.assertFalse(w.tb.keyed)
        self.assertFalse(w.ptt_button.isChecked())
        w.repeat_count_spin.setValue(3)
        w.repeat_interval_spin.setValue(1.0)
        w.ptt_button.setChecked(True)
        self.pump(0.3)
        w.pocsag_text_edit.setText("")                                          # next call is now refused
        self.pump(3.0)
        self.assertFalse(w._repeat_active)
        self.assertIn("Repeat stopped after 1 of 3", w.status_label.text())

    def test_limits(self):
        from pluto_tx import config
        from pluto_tx import gui
        w = gui.MainWindow("x")
        self.assertEqual(w.repeat_count_spin.maximum(), 999)
        self.assertEqual(w.repeat_count_spin.minimum(), 1)
        self.assertEqual(w.repeat_interval_spin.minimum(), config.REPEAT_INTERVAL_RANGE_S[0])


class CliRepeatTests(unittest.TestCase):
    class Events:
        def __init__(self):
            self.events = []

        def emit(self, event, **fields):
            self.events.append((event, fields))

        def error(self, message):
            self.events.append(("error", {"message": message}))

    @classmethod
    def setUpClass(cls):
        from tests import fakes
        fakes.register_tx()

    def session(self, count, interval, **fg_kwargs):
        import argparse
        from pluto_cli import runtime
        fg = txf.PlutoTxFlowgraph(device_type="fake", mode=POCSAG, pocsag_text="R", pocsag_baud=2400, **fg_kwargs)
        fg.start()
        ev = self.Events()
        args = argparse.Namespace(interactive=False, repeat_count=count, repeat_interval=interval, duration=1.0)
        t0 = time.time()
        runtime.run_tx_session(fg, POCSAG, args, ev)
        return ev.events, time.time() - t0

    def test_series_with_pauses(self):
        events, elapsed = self.session(3, 1.0)
        names = [e for e, _f in events]
        self.assertEqual(names.count("keyed"), 3)
        self.assertEqual(names.count("unkeyed"), 3)
        self.assertEqual(names.count("repeat_wait"), 2)
        self.assertEqual([f["repetition"] for e, f in events if e == "keyed"], [1, 2, 3])
        self.assertGreater(elapsed, 2.0)                                       # two pauses of 1 s at least

    def test_single_send_has_no_repeat_fields(self):
        events, _ = self.session(1, 5.0)
        self.assertEqual([e for e, _f in events if e == "repeat_wait"], [])
        self.assertEqual([f for e, f in events if e == "keyed"], [{"mode": POCSAG}])

    def test_refusal_mid_series_stops_cleanly(self):
        import argparse
        from pluto_cli import runtime
        fg = txf.PlutoTxFlowgraph(device_type="fake", mode=POCSAG, pocsag_text="R", pocsag_baud=2400)
        fg.start()
        real_key = fg.key_ptt
        calls = []

        def key_then_refuse():
            calls.append(1)
            if len(calls) == 2:
                raise ValueError("duty cycle used up")
            real_key()

        fg.key_ptt = key_then_refuse
        ev = self.Events()
        runtime.run_tx_session(fg, POCSAG, argparse.Namespace(interactive=False, repeat_count=5, repeat_interval=1.0, duration=1.0), ev)
        names = [e for e, _f in ev.events]
        self.assertEqual(names.count("keyed"), 1)
        self.assertIn("error", names)
        self.assertEqual(names[-1], "shutdown")


if __name__ == "__main__":
    unittest.main()
