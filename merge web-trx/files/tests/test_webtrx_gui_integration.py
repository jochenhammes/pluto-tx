"""The Web-TRX row and its guards inside the real TX and RX main windows:
the row exists, a broken row never stops the app, the Connect guard only
asks when Web-TRX is known to hold that device type, and the automatic
connect at startup is skipped only then. No server, no hardware: the
controller's queries are patched."""
import os
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    import gnuradio  # noqa: F401  -- the main windows import the flowgraphs
    from PyQt5 import QtWidgets
except ImportError:  # pragma: no cover
    QtWidgets = None

from pluto_tx import webtrx_control as wc

ENABLED = {wc.DISABLE_ENV: "on"}
HOLDS_PLUTO = wc.Status(wc.RUNNING, devices={"rx": {"connected": False, "device_type": None},
                                             "tx": {"connected": True, "device_type": "pluto"}})


@unittest.skipIf(QtWidgets is None, "needs GNU Radio and PyQt5")
class GuiIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def setUp(self):
        patcher = mock.patch.dict(os.environ, ENABLED)
        patcher.start()
        self.addCleanup(patcher.stop)

    def apps(self):
        from pluto_advanced_rx import devices as rx_devices
        from pluto_advanced_rx import gui as rx_gui
        from pluto_tx import devices as tx_devices
        from pluto_tx import gui as tx_gui
        return (("tx", tx_gui, tx_devices), ("rx", rx_gui, rx_devices))

    def make_window(self, gui, holds=False):
        """MainWindow("x") with the startup query answered by `holds`."""
        with mock.patch.object(wc.WebTrxControl, "installed", return_value=(True, "")), \
                mock.patch.object(wc.WebTrxControl, "holds", return_value=holds):
            w = gui.MainWindow("x")
        self.addCleanup(w.deleteLater)
        return w

    def test_both_windows_have_the_row(self):
        for name, gui, _ in self.apps():
            with self.subTest(app=name):
                w = self.make_window(gui)
                self.assertIsNotNone(w.webtrx_row)
                self.assertTrue(w.isAncestorOf(w.webtrx_row))
                self.assertEqual(w.webtrx_row.start_button.text(), "Start")

    def test_a_broken_row_never_stops_the_app(self):
        for name, gui, _ in self.apps():
            with self.subTest(app=name), \
                    mock.patch("pluto_tx.webtrx_widget.WebTrxRow", side_effect=RuntimeError("boom")):
                w = gui.MainWindow("x")
                self.addCleanup(w.deleteLater)
                self.assertIsNone(w.webtrx_row)
                labels = [lbl.text() for lbl in w.findChildren(QtWidgets.QLabel)]
                self.assertIn("Web-TRX: not available (boom)", labels)
                with mock.patch.object(w, "_connect") as connect:   # Connect still works as before
                    w._on_connect_clicked()
                connect.assert_called_once()

    def test_connect_with_unknown_status_asks_nothing(self):
        for name, gui, _ in self.apps():
            with self.subTest(app=name):
                w = self.make_window(gui)
                w.webtrx_row.control.last_status = None
                w.webtrx_row.confirm_connect_dialog = mock.Mock(return_value=False)
                with mock.patch.object(w, "_connect") as connect:
                    w._on_connect_clicked()
                connect.assert_called_once()
                w.webtrx_row.confirm_connect_dialog.assert_not_called()

    def test_connect_guard_when_web_trx_holds_the_device(self):
        for name, gui, _ in self.apps():
            with self.subTest(app=name):
                w = self.make_window(gui)
                w.device_type_combo.setCurrentIndex(w.device_type_combo.findData("pluto"))
                w.webtrx_row.control.last_status = HOLDS_PLUTO
                w.webtrx_row.confirm_connect_dialog = mock.Mock(return_value=False)
                with mock.patch.object(w, "_connect") as connect:
                    w._on_connect_clicked()
                connect.assert_not_called()                          # answered "No"
                w.webtrx_row.confirm_connect_dialog.return_value = True
                with mock.patch.object(w, "_connect") as connect:
                    w._on_connect_clicked()
                connect.assert_called_once()                         # answered "Yes"

    def test_connect_guard_ignores_other_device_types(self):
        for name, gui, _ in self.apps():
            with self.subTest(app=name):
                w = self.make_window(gui)
                w.device_type_combo.setCurrentIndex(w.device_type_combo.findData("hackrf"))
                w.webtrx_row.control.last_status = HOLDS_PLUTO
                w.webtrx_row.confirm_connect_dialog = mock.Mock(return_value=False)
                with mock.patch.object(w, "_connect") as connect:
                    w._on_connect_clicked()
                connect.assert_called_once()
                w.webtrx_row.confirm_connect_dialog.assert_not_called()

    def test_startup_connect_skipped_only_when_web_trx_holds_the_device(self):
        for name, gui, devices in self.apps():
            for holds, skipped in ((True, True), (False, False), (None, False)):
                with self.subTest(app=name, holds=holds):
                    device_cls = devices.DEVICE_REGISTRY["pluto"]
                    with mock.patch.object(device_cls, "probe_with_timeout", return_value="unreachable") as probe:
                        w = self.make_window(gui, holds=holds)
                    self.assertIsNone(w.tb)
                    self.assertEqual(probe.called, not skipped)
                    self.assertEqual("not connecting automatically" in w.status_label.text(), skipped)
                    self.assertEqual(w.connect_button.text(), "Connect")
                    self.assertTrue(w.uri_combo.isEnabled())
                    self.assertFalse(w._webtrx_startup)

    def test_startup_check_only_applies_at_startup(self):
        for name, gui, devices in self.apps():
            with self.subTest(app=name):
                w = self.make_window(gui, holds=False)
                device_cls = devices.DEVICE_REGISTRY["pluto"]
                with mock.patch.object(wc.WebTrxControl, "holds", return_value=True) as holds, \
                        mock.patch.object(device_cls, "probe_with_timeout", return_value="unreachable") as probe:
                    w._connect("x")                                  # a later, manual connect
                holds.assert_not_called()
                probe.assert_called_once()

    def test_disabled_integration_changes_nothing(self):
        for name, gui, devices in self.apps():
            with self.subTest(app=name), mock.patch.dict(os.environ, {wc.DISABLE_ENV: "off"}):
                device_cls = devices.DEVICE_REGISTRY["pluto"]
                with mock.patch.object(wc.WebTrxControl, "holds", return_value=True) as holds, \
                        mock.patch.object(device_cls, "probe_with_timeout", return_value="unreachable") as probe:
                    w = gui.MainWindow("x")
                self.addCleanup(w.deleteLater)
                holds.assert_not_called()
                probe.assert_called_once()
                self.assertEqual(w.webtrx_row.state_label.text(), "disabled")

    def test_release_device_disconnects_safely(self):
        from pluto_tx import gui
        w = self.make_window(gui)
        fake_tb = mock.Mock()
        w.tb = fake_tb
        with mock.patch.object(w, "_embed_waterfall"):
            w.webtrx_row._release_device()
        fake_tb.shutdown_safe.assert_called_once()
        self.assertIsNone(w.tb)
        w.webtrx_row._release_device()                               # nothing open: no-op


if __name__ == "__main__":
    unittest.main()
