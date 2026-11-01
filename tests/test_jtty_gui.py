"""JTTY in the Qt apps (offscreen): TX shows what a press would send and refuses before any RF action; RX shows messages as they grow."""
import time
import unittest

from pluto_tx import jtty_codec
from pluto_tx.flowgraph import PlutoTxFlowgraph

JTTY = PlutoTxFlowgraph.MODE_JTTY


@unittest.skipUnless(jtty_codec.available(), "wsjt-codec not installed (install-jtty.sh)")
class JttyGuiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PyQt5 import QtCore, QtWidgets
        from tests import fakes
        QtCore.QStandardPaths.setTestModeEnabled(True)  # keep QSettings out of ~/.config
        fakes.register_tx()
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def pump(self, seconds, until=None):
        end = time.time() + seconds
        while time.time() < end and not (until and until()):
            self.app.processEvents()
            time.sleep(0.01)

    def tx_window(self):
        from pluto_tx import gui
        w = gui.MainWindow("x")
        w.device_type_combo.addItem("fake", "fake")
        w.device_type_combo.setCurrentIndex(w.device_type_combo.findData("fake"))
        w.digimode_combo.setCurrentIndex(w.digimode_combo.findData(JTTY))
        w.mode_tab_widget.setCurrentIndex(1)
        w._rebuild("x", None)
        self.addCleanup(lambda: w.tb is not None and w.tb.shutdown_safe())
        w.tb.set_frequency(432_150_000.0)
        self.assertTrue(w.jtty_group_widget.isVisibleTo(w))
        return w

    def test_choosing_jtty_shows_its_controls_without_a_device(self):
        """As an operator gets there: no flowgraph yet, the mode picked from the menu, the window really on screen."""
        from pluto_tx import gui
        w = gui.MainWindow("x")
        self.addCleanup(w.close)
        w.show()
        w.mode_tab_widget.setCurrentIndex(1)
        w.digimode_combo.setCurrentIndex(w.digimode_combo.findData(JTTY))
        self.app.processEvents()
        self.assertTrue(w.jtty_text_edit.isVisible())
        w.digimode_combo.setCurrentIndex(w.digimode_combo.findData(PlutoTxFlowgraph.MODE_RTTY))
        self.app.processEvents()
        self.assertFalse(w.jtty_text_edit.isVisible())

    def test_preview_shows_what_would_go_out(self):
        w = self.tx_window()
        w.jtty_text_edit.setText("cq da2jh cq")
        self.assertIn("CQ DA2JH CQ", w.jtty_info_label.text())
        self.assertIn("1 frame", w.jtty_info_label.text())
        w.jtty_text_edit.setText("hallo äö")
        self.assertIn("#", w.jtty_info_label.text())
        w.jtty_text_edit.setText("")
        self.assertIn("Not ready", w.jtty_info_label.text())

    def test_press_sends_once_and_unkeys_itself(self):
        w = self.tx_window()
        w.jtty_text_edit.setText("CQ DA2JH CQ")
        self.assertTrue(w._all_ptt_allowed())
        w._key_and_schedule()
        self.assertTrue(w.tb.keyed)
        self.pump(w.tb.jtty_hold_s + 1.0, until=lambda: not w.tb.keyed)
        self.assertFalse(w.tb.keyed)
        self.assertIn("CQ DA2JH CQ", w.jtty_sent_log.toPlainText())

    def test_refused_press_says_why(self):
        w = self.tx_window()
        self.assertFalse(w._all_ptt_allowed())
        self.assertIn("empty", w.status_label.text())
        self.assertFalse(w.tb.keyed)

    def test_rx_shows_a_growing_message(self):
        from pluto_advanced_rx import gui
        w = gui.MainWindow("x")
        self.addCleanup(w.close)
        w.digimode_combo.setCurrentIndex(w.digimode_combo.findData("jtty"))
        self.assertFalse(w.jtty_group_widget.isHidden())
        w._jtty_state.on_update(dict(id=1, text="HEL", state="growing", freq=1500.2, snr=-7, utc_start=1000.0))
        w._render_jtty_table()
        self.assertEqual(w.jtty_table.item(0, 4).text(), "HEL")
        self.assertEqual(w.jtty_table.item(0, 3).text(), "...")
        w._jtty_state.on_update(dict(id=1, text="HELLO", state="complete", freq=1500.2, snr=-7, utc_start=1000.0))
        w._render_jtty_table()
        self.assertEqual((w.jtty_table.rowCount(), w.jtty_table.item(0, 4).text(), w.jtty_table.item(0, 3).text()), (1, "HELLO", ""))
        w._update_jtty_signal_label()
        self.assertEqual(w.jtty_signal_label.text(), "Not connected.")
        w._on_jtty_clear_clicked()
        w._render_jtty_table()
        self.assertEqual(w.jtty_table.rowCount(), 0)


if __name__ == "__main__":
    unittest.main()
