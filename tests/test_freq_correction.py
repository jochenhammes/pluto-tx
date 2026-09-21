"""ppm oscillator correction (TX + RX) and RTL-SDR direct sampling.
Run: python3 -m unittest discover tests"""
import os
import time
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from pluto_advanced_rx import rtl_tcp
from pluto_tx import freq_correction
from tests.fake_rtl_tcp import FakeRtlTcpServer
from tests.test_rtl_tcp import wait_for


class MathTests(unittest.TestCase):
    def test_zero_is_identity_and_sign_convention(self):
        hw = freq_correction.hardware_frequency
        self.assertEqual(hw(433_775_000.0, 0.0), 433_775_000.0)
        self.assertLess(hw(100e6, +10.0), 100e6)  # device runs high -> tune the hardware lower
        self.assertGreater(hw(100e6, -10.0), 100e6)
        self.assertAlmostEqual(hw(100e6, 10.0), 100e6 / (1 + 10e-6))

    def test_a_known_error_scales_with_frequency(self):
        hw = freq_correction.hardware_frequency
        low, high = 100e6 - hw(100e6, 20.0), 1e9 - hw(1e9, 20.0)
        self.assertAlmostEqual(high / low, 10.0, places=3)  # 10x the frequency -> 10x the Hz shift
        # a device measured 56 ppm LOW (carrier shows 432.15 MHz - 24.2 kHz) needs about +24.2 kHz
        self.assertAlmostEqual(hw(432.15e6, -56.0) - 432.15e6, 24_200, delta=10)


class BackendTests(unittest.TestCase):
    def _check(self, device, attr, tuned):
        """`tuned(call)` extracts the Hz from a recorded set_frequency call."""
        mock_dev = mock.MagicMock()
        setattr(device, attr, mock_dev)
        device.set_frequency(433_775_000.0)
        self.assertEqual(tuned(mock_dev.set_frequency.call_args), 433_775_000)  # default 0 ppm
        device.set_frequency_correction_ppm(-20.0)  # re-tunes by itself
        expected = int(433_775_000.0 / (1 - 20e-6))
        self.assertEqual(tuned(mock_dev.set_frequency.call_args), expected)
        self.assertEqual(device.frequency_hz, 433_775_000.0)  # the TRUE frequency is untouched
        device.set_frequency(144_800_000.0)  # the ppm scales with the new frequency
        self.assertEqual(tuned(mock_dev.set_frequency.call_args), int(144_800_000.0 / (1 - 20e-6)))

    def test_rx_backends(self):
        from pluto_advanced_rx.devices.hackrf import HackRFDevice
        from pluto_advanced_rx.devices.pluto import PlutoDevice
        from pluto_advanced_rx.devices.rtlsdr import RtlSdrDevice
        self._check(PlutoDevice("ip:x", 1e8, 2_500_000, None), "_source", lambda c: c[0][0])
        self._check(HackRFDevice("", 1e8, 2_000_000, None), "_source", lambda c: c[0][1])
        self._check(RtlSdrDevice("", 1e8, 2_400_000, None), "_source", lambda c: c[0][1])

    def test_tx_backends(self):
        from pluto_tx.devices.hackrf import HackRFDevice
        self._check(HackRFDevice("", 1e8, 2_000_000, None), "_sink", lambda c: c[0][1])
        from pluto_tx.devices.pluto import PlutoDevice
        with mock.patch("pluto_tx.devices.pluto.PlutoSafety"):  # the real one opens a libiio context
            dev = PlutoDevice("ip:127.0.0.1", 1e8, 2_500_000, 200_000)
        self._check(dev, "_sink", lambda c: c[0][0])

    def test_defaults_are_zero_and_soundcard_has_none(self):
        from pluto_tx.devices.hackrf import HackRFDevice
        from pluto_tx.devices.soundcard import SoundcardDevice
        self.assertEqual(HackRFDevice("", 1e8, 2_000_000, None)._frequency_correction_ppm, 0.0)
        self.assertFalse(SoundcardDevice.supports_frequency_correction)
        self.assertFalse(hasattr(HackRFDevice, "DEFAULT_FREQUENCY_CORRECTION_HZ"))

    def test_rtl_local_direct_sampling_setting(self):
        from pluto_advanced_rx.devices.rtlsdr import RtlSdrDevice
        dev = RtlSdrDevice("", 7_100_000.0, 2_400_000, None)
        dev._source = mock.MagicMock()
        dev.set_direct_sampling(2)
        dev._source.write_setting.assert_called_with("direct_samp", "2")
        dev.set_direct_sampling(0)
        dev._source.write_setting.assert_called_with("direct_samp", "0")
        self.assertEqual(dev._source.set_frequency.call_args[0][1], 7_100_000)  # retuned afterwards


class RtlTcpTests(unittest.TestCase):
    def test_commands_and_replay_after_reconnect(self):
        from pluto_advanced_rx.devices.rtlsdr import RtlSdrDevice
        srv = FakeRtlTcpServer()
        self.addCleanup(srv.close)
        dev = RtlSdrDevice(f"127.0.0.1:{srv.port}", 100_000_000.0, 1_024_000, None)
        dev.set_frequency_correction_ppm(10.0)
        dev.set_direct_sampling(2)
        dev.build_source()
        self.addCleanup(dev.close)
        self.assertTrue(wait_for(lambda: dev.connection_status() == "connected"))
        self.assertTrue(wait_for(lambda: srv.last_param(rtl_tcp.CMD_SET_DIRECT_SAMPLING) == 2))
        self.assertTrue(wait_for(lambda: srv.last_param(rtl_tcp.CMD_SET_FREQ) == int(100e6 / (1 + 10e-6))), "rtl_tcp.CMD_SET_FREQ not seen")
        dev.set_frequency_correction_ppm(0.0)
        self.assertTrue(wait_for(lambda: srv.last_param(rtl_tcp.CMD_SET_FREQ) == 100_000_000))
        dev.set_direct_sampling(0)
        self.assertTrue(wait_for(lambda: srv.last_param(rtl_tcp.CMD_SET_DIRECT_SAMPLING) == 0))
        dev.set_direct_sampling(1)
        srv.commands.clear()
        srv.drop_client()
        self.assertTrue(wait_for(lambda: srv.connections >= 2 and dev.connection_status() == "connected", 8.0))
        self.assertTrue(wait_for(lambda: srv.last_param(rtl_tcp.CMD_SET_DIRECT_SAMPLING) == 1))
        self.assertTrue(wait_for(lambda: srv.last_param(rtl_tcp.CMD_SET_FREQ) == 100_000_000), "rtl_tcp.CMD_SET_FREQ not seen")

    def test_flowgraph_kwargs_reach_the_device(self):
        from pluto_advanced_rx import flowgraph as rxf
        srv = FakeRtlTcpServer()
        self.addCleanup(srv.close)
        fg = rxf.AdvancedRxFlowgraph(uri=f"127.0.0.1:{srv.port}", frequency=7_100_000.0, sample_rate=1_024_000,
                                     device_type="rtlsdr", fft_size=1024, frequency_correction_ppm=25.0,
                                     direct_sampling=2)
        fg.start()
        try:
            self.assertTrue(wait_for(lambda: srv.last_param(rtl_tcp.CMD_SET_DIRECT_SAMPLING) == 2))
            self.assertTrue(wait_for(lambda: srv.last_param(rtl_tcp.CMD_SET_FREQ) == int(7.1e6 / (1 + 25e-6))), "rtl_tcp.CMD_SET_FREQ not seen")
            fg.set_frequency_correction_ppm(0.0)
            self.assertTrue(wait_for(lambda: srv.last_param(rtl_tcp.CMD_SET_FREQ) == 7_100_000))
            fg.set_direct_sampling(0)
            self.assertTrue(wait_for(lambda: srv.last_param(rtl_tcp.CMD_SET_DIRECT_SAMPLING) == 0))
        finally:
            fg.shutdown()


class GuiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PyQt5 import QtWidgets
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def pump(self, s):
        t = time.time()
        while time.time() - t < s:
            self.app.processEvents()
            time.sleep(0.01)

    def test_rx_second_row(self):
        from pluto_advanced_rx import gui
        w = gui.MainWindow("x")
        self.assertEqual(w.ppm_spin.value(), 0.0)
        self.assertTrue(w.ppm_spin.isVisibleTo(w))
        self.assertFalse(w.direct_checkbox.isVisibleTo(w))  # RTL-SDR only
        w.device_type_combo.setCurrentIndex(w.device_type_combo.findData("rtlsdr"))
        self.assertTrue(w.direct_checkbox.isVisibleTo(w))
        self.assertFalse(w.direct_branch_combo.isEnabled())
        w.device_type_combo.setCurrentIndex(w.device_type_combo.findData("audio"))
        self.assertFalse(w.ppm_spin.isVisibleTo(w))  # no LO on a sound card
        self.assertFalse(w.direct_checkbox.isVisibleTo(w))

    def test_rx_direct_sampling_switches_range_and_restores(self):
        from pluto_advanced_rx import gui
        srv = FakeRtlTcpServer()
        self.addCleanup(srv.close)
        w = gui.MainWindow("x")
        w.device_type_combo.setCurrentIndex(w.device_type_combo.findData("rtlsdr"))
        w.freq_spin.setValue(100.0)
        w._connect(f"127.0.0.1:{srv.port}")
        self.addCleanup(lambda: w.tb and w.tb.shutdown())
        self.pump(1.0)
        w.ppm_spin.setValue(12.5)
        self.assertTrue(wait_for(lambda: srv.last_param(rtl_tcp.CMD_SET_FREQ) == int(100e6 / (1 + 12.5e-6))))
        w.direct_checkbox.setChecked(True)  # default branch: Q
        self.pump(0.5)
        self.assertTrue(wait_for(lambda: srv.last_param(rtl_tcp.CMD_SET_DIRECT_SAMPLING) == 2), "rtl_tcp.CMD_SET_DIRECT_SAMPLING not seen")
        self.assertLessEqual(w.freq_spin.maximum(), 28.8)  # HF range
        w.freq_spin.setValue(21.3)
        self.assertAlmostEqual(w.freq_spin.value(), 21.3)  # above 14.4 MHz is allowed (aliased zone)
        w.freq_spin.setValue(gui.DIRECT_SAMPLING_DEFAULT_MHZ)
        self.assertEqual(w.freq_spin.value(), gui.DIRECT_SAMPLING_DEFAULT_MHZ)  # 100 MHz was out of range
        self.assertTrue(wait_for(lambda: srv.last_param(rtl_tcp.CMD_SET_FREQ) == int(7.1e6 / (1 + 12.5e-6))))
        w.direct_branch_combo.setCurrentIndex(w.direct_branch_combo.findData(1))  # I branch
        self.pump(0.3)
        self.assertTrue(wait_for(lambda: srv.last_param(rtl_tcp.CMD_SET_DIRECT_SAMPLING) == 1), "rtl_tcp.CMD_SET_DIRECT_SAMPLING not seen")
        w.direct_checkbox.setChecked(False)
        self.pump(0.3)
        self.assertTrue(wait_for(lambda: srv.last_param(rtl_tcp.CMD_SET_DIRECT_SAMPLING) == 0), "rtl_tcp.CMD_SET_DIRECT_SAMPLING not seen")
        self.assertAlmostEqual(w.freq_spin.value(), 100.0)  # restored
        self.assertGreater(w.freq_spin.maximum(), 1000.0)
        # a rebuild (bandwidth change) re-applies both options
        w.ppm_spin.setValue(3.0)
        kw = w._current_gain_kwargs(gui.devices.DEVICE_REGISTRY["rtlsdr"])
        self.assertEqual((kw["frequency_correction_ppm"], kw["direct_sampling"]), (3.0, 0))

    def test_tx_two_column_layout(self):
        from pluto_tx import gui
        w = gui.MainWindow("x")
        w.show()
        self.addCleanup(w.close)
        self.pump(0.3)
        panel = w.waterfall_container.parentWidget()
        left_edge = w.mode_tab_widget.geometry().right()
        self.assertGreater(panel.geometry().left(), left_edge)                       # waterfall column is to the right
        self.assertEqual(panel.width(), gui.WATERFALL_PANEL_MIN_WIDTH_PX)          # about half the old full-window width
        self.assertLess(panel.width(), w.mode_tab_widget.width() * 0.6)
        w.resize(w.width() + 400, w.height() + 200)                                  # first room: controls reach their natural width
        self.pump(0.3)
        tab_width, panel_width = w.mode_tab_widget.width(), panel.width()
        w.resize(w.width() + 600, w.height())
        self.pump(0.3)
        self.assertEqual(w.mode_tab_widget.width(), tab_width)                       # controls keep their width...
        self.assertGreater(panel.width(), panel_width + 500)                         # ...the waterfall takes the rest
        for i in range(w.mode_tab_widget.count()):                                  # every tab stays in the left column
            w.mode_tab_widget.setCurrentIndex(i)
            self.pump(0.05)
            self.assertLess(w.mode_tab_widget.geometry().right(), panel.geometry().left())

    def test_tx_correction_field(self):
        from pluto_tx import gui
        w = gui.MainWindow("x")
        self.assertEqual(w.freq_correction_spin.value(), 0.0)
        for dev, visible in (("pluto", True), ("hackrf", True), ("soundcard", False)):
            w.device_type_combo.setCurrentIndex(w.device_type_combo.findData(dev))
            self.assertEqual(w.freq_correction_spin.isVisibleTo(w), visible, dev)
            self.assertEqual(w.freq_correction_spin.value(), 0.0, dev)  # default 0 -- no more 24.2 kHz for HackRF


if __name__ == "__main__":
    unittest.main()
