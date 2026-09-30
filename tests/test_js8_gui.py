"""J5: JS8 in the Qt apps (offscreen): the TX group builds the message and sends its frames one keying each,
stop/E-STOP cancel the rest; the RX group shows frames, messages and stations."""
import time
import unittest

from pluto_advanced_rx.js8_decoder import Js8Decode
from pluto_tx import js8_message as M, js8_phy as P
from pluto_tx.flowgraph import PlutoTxFlowgraph

JS8 = PlutoTxFlowgraph.MODE_JS8


class Js8GuiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PyQt5 import QtCore, QtWidgets
        from tests import fakes
        QtCore.QStandardPaths.setTestModeEnabled(True)                           # keep QSettings out of ~/.config
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
        self.assertGreaterEqual(w.digimode_combo.findData(JS8), 0)
        w.digimode_combo.setCurrentIndex(w.digimode_combo.findData(JS8))
        w.mode_tab_widget.setCurrentIndex(1)
        w._rebuild("x", None)
        self.addCleanup(lambda: w.tb is not None and w.tb.shutdown_safe())
        w.tb.set_frequency(144_178_000.0)
        self.assertTrue(w.js8_group_widget.isVisibleTo(w))
        w.js8_mycall_edit.setText("da2jh")
        w.js8_grid_edit.setText("jo31")
        w.js8_speed_combo.setCurrentIndex(w.js8_speed_combo.findData(P.TURBO))
        return w

    def test_preview_counts_frames(self):
        w = self.tx_window()
        w.js8_kind_combo.setCurrentIndex(w.js8_kind_combo.findData("allcall"))
        w.js8_text_edit.setText("js8 sequence test de da2jh")
        n = len(M.build_frames("DA2JH", "JO31", "@ALLCALL JS8 SEQUENCE TEST DE DA2JH", P.TURBO))
        self.assertIn(f"{n} frames", w.js8_preview_label.text())
        self.assertIn("DA2JH: @ALLCALL  JS8 SEQUENCE TEST DE DA2JH", w.js8_preview_label.text())
        w.js8_kind_combo.setCurrentIndex(w.js8_kind_combo.findData("ack"))
        self.assertIn("Not sendable", w.js8_preview_label.text())                # no "To:"
        w.js8_to_edit.setText("dl1abc")
        self.assertIn("1 frame ", w.js8_preview_label.text())

    def test_send_one_frame_message(self):
        w = self.tx_window()
        w.js8_kind_combo.setCurrentIndex(w.js8_kind_combo.findData("snr_query"))
        w.js8_to_edit.setText("DL1ABC")
        w.ptt_button.setChecked(True)
        self.assertTrue(w._js8_active)
        self.assertEqual(w.tb.js8_frames, M.build_frames("DA2JH", "JO31", "DL1ABC SNR?", P.TURBO))
        self.assertIn("JS8 frame 1/1", w.ptt_button.text())
        self.pump(12, until=lambda: w.tb.keyed)
        self.assertTrue(w.tb.keyed)
        self.pump(8, until=lambda: not w._js8_active)
        self.assertFalse(w.tb.keyed)
        self.assertEqual(w.ptt_button.text(), "PTT (click to send)")
        self.assertIn("DA2JH: DL1ABC SNR?", w.js8_sent_log.toPlainText())
        self.assertIn("sent (1 frame)", w.status_label.text())

    def test_early_timer_does_not_cut_a_frame(self):
        # J7: Qt's coarse timers may fire up to 5 % early, and an early unkey aborts the whole message
        w = self.tx_window()
        w.js8_kind_combo.setCurrentIndex(w.js8_kind_combo.findData("allcall"))
        w.js8_text_edit.setText("JS8 SEQUENCE TEST DE DA2JH")
        w.ptt_button.setChecked(True)
        n = len(w._js8_plan)
        self.assertGreaterEqual(n, 2)
        self.pump(12, until=lambda: w.tb.keyed)
        w._js8_frame_done(0, w.tb, w._js8_epoch)                               # "fires" long before the end
        self.assertTrue(w.tb.keyed)
        self.assertEqual(w._js8_next, 0)
        self.pump(w._js8_plan[-1][1] - time.time() + 8, until=lambda: not w._js8_active)
        self.assertEqual(w.tb.js8_source.frames_sent, set(range(n)))
        self.assertIn(f"sent ({n} frames)", w.status_label.text())

    def test_stop_after_first_frame_sends_no_more(self):
        w = self.tx_window()
        w.js8_kind_combo.setCurrentIndex(w.js8_kind_combo.findData("allcall"))
        w.js8_text_edit.setText("JS8 SEQUENCE TEST DE DA2JH")
        w.ptt_button.setChecked(True)
        self.pump(12, until=lambda: w.tb.keyed)
        self.pump(8, until=lambda: not w.tb.keyed)                              # frame 1 done
        self.assertTrue(w._js8_active)
        w.ptt_button.setChecked(False)                                           # operator stops
        self.assertFalse(w._js8_active)
        self.pump(8)
        self.assertFalse(w.tb.keyed)
        self.assertEqual(set(w.tb.js8_source.frame_started_at), {0})
        self.assertEqual(w.js8_sent_log.toPlainText().count("\n"), 0)             # one line logged

    def test_estop_while_waiting(self):
        w = self.tx_window()
        w.js8_kind_combo.setCurrentIndex(w.js8_kind_combo.findData("cq"))
        w.ptt_button.setChecked(True)
        self.assertTrue(w._js8_active)
        w.estop_button.setChecked(True)
        self.assertFalse(w._js8_active)
        self.pump(10)
        self.assertFalse(w.tb.keyed)
        self.assertEqual(w.js8_sent_log.toPlainText(), "")
        w.estop_button.setChecked(False)

    def test_rx_group(self):
        from pluto_advanced_rx import gui
        w = gui.MainWindow("x")
        w.digimode_combo.setCurrentIndex(w.digimode_combo.findData("js8"))
        self.assertFalse(w.js8_group_widget.isHidden())
        self.assertTrue(w.ft8_group_widget.isHidden())
        frames = M.build_frames("DL1ABC", "JO62", "@ALLCALL HELLO FROM DL1ABC", P.NORMAL)
        t0 = 1_000_000_005.0
        for i, (f, b) in enumerate(frames):
            d = M.decode_frame(f, b, P.NORMAL)
            w._js8_state.on_decodes(t0 + 15 * i, P.NORMAL,
                                    [Js8Decode(f, b, P.NORMAL, -12.0, 0.1, 812.0, d["message"], d["frame_type"])])
        hb = M.build_frames("DK2XY", "JO40", "DK2XY: HEARTBEAT JO40", P.NORMAL)[0]
        d = M.decode_frame(*hb, P.NORMAL)
        w._js8_state.on_decodes(t0 + 60, P.NORMAL, [Js8Decode(*hb, P.NORMAL, -3.0, 0.0, 700.0, d["message"],
                                                              d["frame_type"])])
        w._render_js8_tables()
        self.assertEqual(w.js8_messages_table.rowCount(), 2)
        self.assertEqual(w.js8_messages_table.item(0, 5).text(), "DL1ABC: @ALLCALL  HELLO FROM DL1ABC")
        self.assertEqual({w.js8_stations_table.item(r, 0).text() for r in range(2)}, {"DL1ABC", "DK2XY"})
        grid = {w.js8_stations_table.item(r, 0).text(): w.js8_stations_table.item(r, 1).text() for r in range(2)}
        self.assertEqual(grid["DK2XY"], "JO40")
        self.assertEqual(w.js8_activity_table.rowCount(), 2)
        w._on_js8_row_double_clicked(w.js8_messages_table, 0)
        self.assertEqual(self.app.clipboard().text(), "DL1ABC")


if __name__ == "__main__":
    unittest.main()
