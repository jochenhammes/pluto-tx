"""The X6200 device, its CAT client and its PTT guard, against a fake rigctld with the measured behaviour of the real radio."""
import os
import subprocess
import sys
import threading
import time
import unittest
from unittest import mock

from tests.fake_rigctld import FakeRigctld

from pluto_tx import rigctl
from pluto_tx.rigctl import RigctlClient, RigError, RigTimeout

GUARD = os.path.join(os.path.dirname(rigctl.__file__), "rig_guard.py")


def wait_until(condition, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if condition():
            return True
        time.sleep(0.02)
    return condition()


class Base(unittest.TestCase):
    def setUp(self):
        self.rig = FakeRigctld(ptt_delay_s=0.05)
        self.addCleanup(self.rig.close)


class ClientTests(Base):
    def make(self, **kw):
        c = RigctlClient(port=self.rig.port, **kw)
        self.addCleanup(c.close)
        return c

    def test_round_trips(self):
        c = self.make()
        c.set_frequency(14_090_000)
        self.assertEqual(c.get_frequency(), 14_090_000.0)
        c.set_mode("PKTUSB")
        self.assertEqual(c.get_mode(), "PKTUSB")
        self.assertEqual(c.get_level("STRENGTH"), -29.0)
        c.set_ptt(True)
        self.assertTrue(wait_until(c.get_ptt))
        c.set_ptt(False)
        self.assertTrue(wait_until(lambda: not c.get_ptt()))

    def test_error_code_becomes_rig_error(self):
        with self.assertRaisesRegex(RigError, "error -5"):
            self.make().set_level("RFPOWER", 0.5)

    def test_no_answer_is_a_timeout(self):
        c = self.make(timeout=0.2)
        c.get_frequency()
        self.rig.silent = True
        with self.assertRaises(RigTimeout):
            c.get_frequency()

    def test_a_lost_answer_is_asked_again(self):
        c = self.make(timeout=0.2)
        c.get_frequency()
        self.rig.silent = True
        threading.Timer(0.1, lambda: setattr(self.rig, "silent", False)).start()
        self.assertEqual(c.get_frequency(), self.rig.frequency)

    def test_reconnects_after_a_broken_connection(self):
        c = self.make()
        c.get_frequency()
        self.rig.drop_next = True
        self.assertEqual(c.get_frequency(), self.rig.frequency)

    def test_nothing_listening(self):
        port = self.rig.port
        self.rig.close()
        self.rig = FakeRigctld()
        with self.assertRaisesRegex(RigError, "not reachable"):
            RigctlClient(port=port, timeout=0.3).connect()


class ManagerTests(Base):
    def test_uses_a_running_rigctld(self):
        manager = rigctl.RigctldManager("/dev/null", 3091, self.rig.port, program="/nonexistent")
        manager.ensure_running()
        self.assertIsNone(manager.process)

    def test_says_when_the_radio_is_silent(self):
        """Seen when the battery ran out: rigctld still listens, the radio no longer answers."""
        self.rig.silent = True
        manager = rigctl.RigctldManager("/dev/null", 3091, self.rig.port, program="/nonexistent")
        with self.assertRaisesRegex(RigError, "is it switched on"):
            manager.ensure_running()

    def test_says_when_rigctld_is_missing(self):
        port = self.rig.port
        self.rig.close()
        self.rig = FakeRigctld()
        manager = rigctl.RigctldManager("/dev/null", 3091, port, program=None)
        with mock.patch.object(rigctl, "_find_rigctld", return_value=None):
            with self.assertRaisesRegex(RigError, "libhamlib-utils"):
                manager.ensure_running()


class GuardTests(Base):
    def guard(self):
        g = subprocess.Popen([sys.executable, GUARD, str(self.rig.port)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        def end():
            g.kill()
            g.wait()
            g.stdout.close()
            if not g.stdin.closed:
                g.stdin.close()
        self.addCleanup(end)
        return g

    def tell(self, g, line):
        g.stdin.write(line + "\n")
        g.stdin.flush()

    def key(self):
        self.rig.answer("T 1")
        self.assertTrue(wait_until(lambda: self.rig.ptt))

    def test_releases_when_the_app_dies(self):
        g = self.guard()
        self.tell(g, "key 60")
        self.key()
        g.stdin.close()  # what the kernel does on kill -9 as well
        self.assertTrue(wait_until(lambda: not self.rig.ptt))
        self.assertEqual(g.wait(timeout=5), 0)

    def test_time_limit(self):
        g = self.guard()
        self.key()
        self.tell(g, "key 0.3")
        self.assertTrue(wait_until(lambda: not self.rig.ptt))
        self.assertEqual(g.stdout.readline().strip(), "released timeout")
        g.stdin.close()

    def test_unkey_cancels_the_limit(self):
        g = self.guard()
        self.tell(g, "key 0.3")
        self.tell(g, "unkey")
        self.key()
        time.sleep(0.8)
        self.assertTrue(self.rig.ptt)
        g.stdin.close()

    def test_survives_sigterm_until_the_pipe_closes(self):
        g = self.guard()
        self.tell(g, "key 60")
        self.key()
        time.sleep(0.4)  # past the handlers' installation
        g.terminate()
        time.sleep(0.2)
        self.assertIsNone(g.poll())
        g.stdin.close()
        self.assertTrue(wait_until(lambda: not self.rig.ptt))


class ParseTests(unittest.TestCase):
    def test_connection(self):
        from pluto_tx.devices import x6200 as d
        self.assertEqual(d.parse_connection("/dev/ttyACM1|monitor:node"), ("/dev/ttyACM1", "monitor:node", 3091))
        self.assertEqual(d.parse_connection("/dev/ttyACM1@3087|"), ("/dev/ttyACM1", "", 3087))
        self.assertEqual(d.parse_connection(""), ("", "", 3091))
        self.assertEqual(d.rigctld_address("rigctld://localhost:4533"), ("localhost", 4533))
        self.assertEqual(d.rigctld_address("rigctld://"), ("127.0.0.1", 4532))
        self.assertIsNone(d.rigctld_address("/dev/ttyACM1"))


class ResolveTests(unittest.TestCase):
    def test_audio_is_never_left_blank(self):
        from pluto_tx.devices import x6200 as d
        with mock.patch.object(d, "audio_next_to", return_value="") as nearby, mock.patch.object(d, "_scan_serial_ports", return_value=["/dev/ttyACM1"]):
            with self.assertRaisesRegex(RigError, "no sound card"):
                d.resolve_rig_and_audio("", "", "Audio/Sink")
            nearby.assert_called_once_with("/dev/ttyACM1", "Audio/Sink")
            with self.assertRaisesRegex(RigError, "no sound card"):  # a remote rigctld has no USB hub to look at
                d.resolve_rig_and_audio("rigctld://h:1", "", "Audio/Sink")
        with mock.patch.object(d, "audio_next_to", return_value="plughw:CARD=X,DEV=0"), mock.patch.object(d, "_scan_serial_ports", return_value=["/dev/ttyACM1"]):
            self.assertEqual(d.resolve_rig_and_audio("", "", "Audio/Sink"), ("/dev/ttyACM1", "plughw:CARD=X,DEV=0"))
        self.assertEqual(d.resolve_rig_and_audio("/dev/x", "a", "Audio/Sink"), ("/dev/x", "a"))

    def test_no_radio(self):
        from pluto_tx.devices import x6200 as d
        with mock.patch.object(d, "_scan_serial_ports", return_value=[]):
            with self.assertRaisesRegex(RigError, "no X6200"):
                d.resolve_rig_and_audio("", "", "Audio/Sink")


class DeviceTests(Base):
    def make(self, frequency=14_090_000.0):
        from pluto_tx.devices.x6200 import X6200Device
        dev = X6200Device(f"rigctld://127.0.0.1:{self.rig.port}|monitor:node", frequency, 2_500_000, None)
        with mock.patch("pluto_tx.devices.x6200._set_pipewire_volume") as volume:
            dev.prepare_for_start()
            volume.assert_called_once_with("node")
        self.addCleanup(dev.close)
        dev.select_mode("jtty")
        dev.set_power("rfpower", 5.0)
        return dev

    def test_is_a_transceiver_on_the_audio_path(self):
        from pluto_tx.devices.x6200 import X6200Device
        self.assertTrue(X6200Device.is_audio_only())
        self.assertTrue(X6200Device.has_frequency_control())
        self.assertTrue(X6200Device.needs_ptt_control)
        for mode in ("jtty", "ft8", "js8", "rtty", "psk31", "digitext", "rade", "ssb", "lsb"):
            self.assertTrue(X6200Device.supports_mode(mode), mode)
        for mode in ("fm", "pocsag", "aprs", "m17", "meshcore", "meshtastic", "baseband", "filebroadcast", "freedv"):
            self.assertFalse(X6200Device.supports_mode(mode), mode)

    def test_keying_order_power_and_mode_before_ptt_seen_before_return(self):
        self.rig.ptt_delay_s = 0.2
        dev = self.make()
        self.rig.log.clear()
        dev.pre_key()
        self.assertTrue(self.rig.ptt)   # it has been seen, so the audio may be unmuted now
        order = [c.split()[0] for _t, c in self.rig.log]
        self.assertLess(order.index("F"), order.index("T"))
        self.assertLess(order.index("M"), order.index("T"))
        self.assertEqual(self.rig.mode, "PKTUSB")
        self.assertAlmostEqual(self.rig.rf_power, 0.05, delta=0.005)
        dev.post_unkey()
        self.assertFalse(self.rig.ptt)

    def test_lower_sideband_uses_its_data_mode(self):
        dev = self.make()
        dev.select_mode("lsb")
        dev.pre_key()
        self.assertEqual(self.rig.mode, "PKTLSB")
        dev.post_unkey()

    def test_refusals_come_before_the_ptt(self):
        cases = [("outside the radio's range", 432_150_000.0, {}), ("outside the amateur bands", 15_000_000.0, {})]
        for why, frequency, _attrs in cases:
            dev = self.make(frequency)
            self.rig.log.clear()
            with self.assertRaisesRegex(RigError, why):
                dev.pre_key()
            self.assertEqual(self.rig.commands("T"), [], why)

    def test_radio_on_another_frequency_is_refused(self):
        dev = self.make()
        self.rig.frequency = 14_200_000.0
        self.rig.stuck_frequency = True
        self.rig.log.clear()
        with self.assertRaisesRegex(RigError, "stayed on"):
            dev.pre_key()
        self.assertEqual(self.rig.commands("T"), [])

    def test_power_that_does_not_arrive_is_refused(self):
        dev = self.make()
        self.rig.stuck_power = True
        self.rig.rf_power = 1.0
        self.rig.log.clear()
        with self.assertRaisesRegex(RigError, "100 % power"):
            dev.pre_key()
        self.assertEqual(self.rig.commands("T"), [])

    def wait_for_power(self, value):
        end = time.monotonic() + 3.0
        while abs(self.rig.rf_power - value) > 0.005 and time.monotonic() < end:
            time.sleep(0.02)
        self.assertAlmostEqual(self.rig.rf_power, value, delta=0.005)

    def test_power_follows_without_blocking_and_once_per_drag(self):
        dev = self.make()
        self.wait_for_power(0.05)
        self.rig.log.clear()
        for pct in (6.0, 7.0, 8.0, 9.0):
            dev.set_power("rfpower", pct)
        self.assertEqual(self.rig.commands("L"), [])   # nothing on the caller's thread
        self.wait_for_power(0.09)
        self.assertEqual(len(self.rig.commands("L")), 1)
        self.assertEqual(self.rig.commands("T"), [])

    def test_same_power_is_not_written_again(self):
        dev = self.make()
        self.wait_for_power(0.05)
        self.rig.log.clear()
        dev.set_power("rfpower", 5.0)
        time.sleep(0.4)
        self.assertEqual(self.rig.commands("L"), [])

    def test_keying_right_after_a_change_has_the_new_power(self):
        dev = self.make()
        dev.set_power("rfpower", 8.0)
        dev.pre_key()
        self.assertAlmostEqual(self.rig.rf_power, 0.08, delta=0.005)
        dev.post_unkey()
        time.sleep(0.4)
        self.assertEqual(len(self.rig.commands("L")), 1)

    def test_a_stale_first_read_is_not_a_refusal(self):
        dev = self.make()
        self.rig.stale_reads_after_write = 1
        dev.set_power("rfpower", 8.0)
        dev.pre_key()
        self.assertAlmostEqual(self.rig.rf_power, 0.08, delta=0.005)
        dev.post_unkey()

    def test_status_does_not_wait_out_a_power_write(self):
        dev = self.make()
        first = dev.read_hw_state()
        dev._send_power(5.0)
        self.rig.log.clear()
        self.assertEqual(dev.read_hw_state(), first)
        self.assertEqual(self.rig.log, [])

    def test_no_keying_without_the_guard(self):
        dev = self.make()
        dev.close()
        self.rig.log.clear()
        with self.assertRaisesRegex(RigError, "guard"):
            dev.pre_key()
        self.assertEqual(self.rig.commands("T"), [])

    def test_off_value_leaves_the_radio_alone(self):
        """The radio goes no lower than 3/255, so asking for 0 % could only fail. Its PTT is what keeps it dark."""
        dev = self.make()
        self.wait_for_power(0.05)
        self.rig.log.clear()
        dev.set_power("rfpower", dev.primary_stage.off_value)
        time.sleep(0.4)
        self.assertEqual(self.rig.commands("L"), [])
        self.assertAlmostEqual(self.rig.rf_power, 0.05, delta=0.005)

    def test_slider_minimum_is_what_the_radio_reaches(self):
        dev = self.make()
        dev.set_power("rfpower", dev.primary_stage.min_value)
        dev.pre_key()
        self.assertAlmostEqual(self.rig.rf_power, 3 / 255, delta=0.001)
        dev.post_unkey()

    def test_radio_that_refuses_to_transmit(self):
        dev = self.make()
        self.rig.refuse_ptt = True
        with self.assertRaisesRegex(RigError, "refused to transmit"):
            dev.pre_key()
        self.assertFalse(self.rig.ptt)
        self.assertIn("T 0", self.rig.commands("T"))

    def test_force_safe_state_never_raises_and_releases(self):
        dev = self.make()
        dev.pre_key()
        dev.force_safe_state()
        self.assertTrue(wait_until(lambda: not self.rig.ptt))
        self.rig.silent = True
        t = time.monotonic()
        dev.force_safe_state()
        self.assertLess(time.monotonic() - t, 2.0)

    def test_close_releases_through_the_guard(self):
        dev = self.make()
        self.rig.answer("T 1")
        self.assertTrue(wait_until(lambda: self.rig.ptt))
        dev.close()
        self.assertFalse(self.rig.ptt)

    def test_state_for_the_display(self):
        dev = self.make()
        rx = dev.read_hw_state()
        self.assertEqual((rx["frequency_hz"], rx["ptt"], rx["s_meter_db"]), (14_090_000.0, False, -29.0))
        dev.pre_key()
        dev._cat_busy_until = 0.0
        tx = dev.read_hw_state()
        self.assertEqual((tx["ptt"], tx["swr"]), (True, 1.2))
        dev.post_unkey()
        self.rig.silent = True
        dev._client.timeout = 0.2
        dev._client._drop()
        self.assertIn("error", dev.read_hw_state())

    def test_probe(self):
        from pluto_tx.devices.x6200 import X6200Device
        self.assertIsNone(X6200Device.probe_with_timeout(f"rigctld://127.0.0.1:{self.rig.port}|x"))
        self.assertIsInstance(X6200Device.probe_with_timeout("rigctld://127.0.0.1:1|x", 0.3), RigError)


class FlowgraphTests(Base):
    """The real flowgraph, with the radio's sound card replaced by a null sink."""

    def make(self, mode, **kw):
        from gnuradio import blocks, gr
        from pluto_tx import flowgraph as txf
        with mock.patch("pluto_tx.audio_devices.open_output_device", side_effect=lambda *a: blocks.null_sink(gr.sizeof_float)), \
                mock.patch("pluto_tx.devices.x6200._set_pipewire_volume"):
            fg = txf.PlutoTxFlowgraph(device_type="x6200", connection=f"rigctld://127.0.0.1:{self.rig.port}|monitor:n",
                                      frequency=14_090_000.0, mode=mode, **kw)
        self.addCleanup(fg.shutdown_safe)
        return fg

    def test_jtty_keys_after_the_radio_is_ready_and_unkeys(self):
        from pluto_tx import flowgraph as txf, jtty_codec
        if not jtty_codec.available():
            self.skipTest("wsjt-codec not installed")
        fg = self.make(txf.PlutoTxFlowgraph.MODE_JTTY, jtty_text="CQ DA2JH CQ")
        fg.set_target_power(4.0)
        fg.start()
        time.sleep(0.2)
        self.rig.log.clear()
        fg.key_ptt()
        self.assertTrue(fg.keyed)
        self.assertTrue(self.rig.ptt)
        self.assertEqual(self.rig.mode, "PKTUSB")
        self.assertAlmostEqual(self.rig.rf_power, 0.04, delta=0.005)
        time.sleep(fg.jtty_hold_s)
        fg.unkey_ptt()
        self.assertFalse(fg.keyed)
        self.assertFalse(self.rig.ptt)

    def test_follows_the_front_panel_without_writing_back(self):
        from pluto_tx import flowgraph as txf
        fg = self.make(txf.PlutoTxFlowgraph.MODE_SSB)
        self.assertIsNone(fg.follow_device_frequency())
        self.rig.log.clear()
        self.rig.frequency = 14_100_250.0
        self.assertEqual(fg.follow_device_frequency(), 14_100_250.0)
        self.assertEqual(fg.nominal_freq_hz, 14_100_250.0)
        self.assertIsNone(fg.follow_device_frequency())
        self.assertEqual(self.rig.commands("F"), [])
        self.rig.frequency = 70_000_000.0   # outside what this app drives: stays where it was
        self.assertIsNone(fg.follow_device_frequency())
        self.rig.frequency = 14_100_250.0

    def test_keying_keeps_a_frequency_just_set_on_the_radio(self):
        from pluto_tx import flowgraph as txf
        fg = self.make(txf.PlutoTxFlowgraph.MODE_SSB)
        fg.start()
        time.sleep(0.2)
        self.rig.frequency = 14_100_250.0
        fg.key_ptt()
        self.assertEqual(self.rig.frequency, 14_100_250.0)
        self.assertEqual(fg.follow_device_frequency(), 14_100_250.0)  # reported to the display after all
        self.rig.frequency = 14_101_000.0                             # while keyed: taken over only after it
        self.assertIsNone(fg.follow_device_frequency())
        fg.unkey_ptt()
        self.assertIsNone(fg.follow_device_frequency())    # CAT is still silent after the power write before keying
        fg.device._cat_busy_until = 0.0
        self.assertEqual(fg.follow_device_frequency(), 14_101_000.0)

    def test_power_slider_reaches_the_radio_unkeyed_within_the_ceiling(self):
        from pluto_tx import flowgraph as txf
        fg = self.make(txf.PlutoTxFlowgraph.MODE_SSB)
        for value, expected in ((4.0, 0.04), (50.0, fg.power_ceiling / 100.0)):
            fg.set_target_power(value)
            end = time.monotonic() + 3.0
            while abs(self.rig.rf_power - expected) > 0.005 and time.monotonic() < end:
                time.sleep(0.02)
            self.assertAlmostEqual(self.rig.rf_power, expected, delta=0.005)
        self.assertFalse(self.rig.commanded_ptt)

    def test_band_check_applies_unlike_on_a_plain_sound_card(self):
        from pluto_tx import flowgraph as txf, jtty_codec
        if not jtty_codec.available():
            self.skipTest("wsjt-codec not installed")
        fg = self.make(txf.PlutoTxFlowgraph.MODE_JTTY, jtty_text="TEST")
        self.assertIsNone(fg.jtty_problem())
        fg.set_frequency(15_000_000.0)
        self.assertIn("amateur bands", fg.jtty_problem())

    def test_a_refused_ptt_leaves_everything_dark(self):
        from pluto_tx import flowgraph as txf, jtty_codec
        if not jtty_codec.available():
            self.skipTest("wsjt-codec not installed")
        fg = self.make(txf.PlutoTxFlowgraph.MODE_JTTY, jtty_text="TEST")
        fg.start()
        time.sleep(0.2)
        self.rig.refuse_ptt = True
        with self.assertRaisesRegex(ValueError, "refused to transmit"):
            fg.key_ptt()
        self.assertFalse(fg.keyed)
        self.assertEqual(fg.jtty_audio_gain.k(), 0.0)

    def test_ssb_voice_goes_out_as_audio_in_the_data_mode(self):
        from pluto_tx import flowgraph as txf
        for mode, rig_mode in ((txf.PlutoTxFlowgraph.MODE_SSB, "PKTUSB"), (txf.PlutoTxFlowgraph.MODE_LSB, "PKTLSB")):
            with self.subTest(rig_mode=rig_mode):
                fg = self.make(mode)
                fg.set_target_power(3.0)
                fg.start()
                time.sleep(0.2)
                self.assertEqual(fg.ssb_audio_gain.k(), 0.0)
                fg.key_ptt()
                self.assertEqual(self.rig.mode, rig_mode)
                self.assertGreater(fg.ssb_audio_gain.k(), 0.0)
                self.assertTrue(self.rig.ptt)
                fg.unkey_ptt()
                self.assertEqual(fg.ssb_audio_gain.k(), 0.0)
                self.assertFalse(self.rig.ptt)
                fg.shutdown_safe()

    def test_one_shot_sources_wait_for_the_slow_keying(self):
        from pluto_tx import config, flowgraph as txf
        fg = self.make(txf.PlutoTxFlowgraph.MODE_RTTY)
        fg.set_rtty_text("TEST DE DA2JH")
        fg._ensure_rtty_audio()
        lead = int(fg.device.tx_start_loss_s * config.AUDIO_RATE)
        self.assertGreater(lead, 0)
        self.assertEqual(float(abs(fg._rtty_audio[:lead]).max()), 0.0)
        self.assertGreater(float(abs(fg._rtty_audio[lead:lead + 4800]).max()), 0.1)

    def test_an_unsupported_mode_does_not_fall_back_to_fm(self):
        from pluto_tx import flowgraph as txf
        fg = self.make(txf.PlutoTxFlowgraph.MODE_FM)
        self.assertEqual(fg.mode, txf.PlutoTxFlowgraph.MODE_RTTY)


class GuiTests(Base):
    """The TX app with the X6200 chosen: RF controls visible although its signal is audio, impossible modes grey."""

    @classmethod
    def setUpClass(cls):
        from PyQt5 import QtCore, QtWidgets
        QtCore.QStandardPaths.setTestModeEnabled(True)
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def test_display_follows_the_front_panel(self):
        from pluto_tx import gui
        from pluto_tx.devices.x6200 import X6200Device
        rig = FakeRigctld()
        self.addCleanup(rig.close)
        dev = X6200Device(f"rigctld://127.0.0.1:{rig.port}|monitor:node", 14_090_000.0, 2_500_000, None)
        with mock.patch("pluto_tx.devices.x6200._set_pipewire_volume"):
            dev.prepare_for_start()
        self.addCleanup(dev.close)
        w = gui.MainWindow("x")
        self.addCleanup(w.close)
        w.device_type_combo.setCurrentIndex(w.device_type_combo.findData("x6200"))
        tb = mock.Mock(device=dev)
        tb.follow_device_frequency.return_value = 14_100_250.0
        tb.compressor.gain_reduction_db.return_value = 0.0
        w.tb = tb
        w._tick()
        self.assertAlmostEqual(w.freq_spin.value(), 14.10025, places=6)
        tb.set_frequency.assert_not_called()

    def test_ssb_stays_selectable_after_another_mode(self):
        """Reported on the real radio: it counted as audio-only here, which greys out SSB and forces RADE."""
        from pluto_tx import gui
        from pluto_tx.flowgraph import PlutoTxFlowgraph as F
        w = gui.MainWindow("x")
        self.addCleanup(w.close)
        w.show()
        w.device_type_combo.setCurrentIndex(w.device_type_combo.findData("x6200"))
        self.app.processEvents()
        for other_combo, other in ((w.digimode_combo, F.MODE_RTTY), (w.mode_combo, F.MODE_RADE)):
            other_combo.setCurrentIndex(other_combo.findData(other))
            self.app.processEvents()
            self.assertEqual(w._current_mode, other)
            w.mode_tab_widget.setCurrentIndex(0)   # the combo lives on this tab, and showing it activates its mode
            for mode in (F.MODE_SSB, F.MODE_LSB):
                self.assertTrue(w.mode_combo.model().item(w.mode_combo.findData(mode)).isEnabled(), mode)
                w.mode_combo.setCurrentIndex(w.mode_combo.findData(mode))
                self.app.processEvents()
                self.assertEqual(w._current_mode, mode)
                w._sync_mode_combo_availability()   # runs again on connecting
                self.assertEqual(w._current_mode, mode)
        for mode in (F.MODE_FM, F.MODE_M17, F.MODE_FREEDV):
            self.assertFalse(w.mode_combo.model().item(w.mode_combo.findData(mode)).isEnabled(), mode)
        for mode in (F.MODE_MESHTASTIC, F.MODE_MESHCORE, F.MODE_POCSAG, F.MODE_APRS):
            self.assertFalse(w.digimode_combo.model().item(w.digimode_combo.findData(mode)).isEnabled(), mode)

    def test_rearm_after_estop(self):
        from pluto_tx import gui
        from pluto_tx.devices.x6200 import X6200Device
        rig = FakeRigctld()
        self.addCleanup(rig.close)
        dev = X6200Device(f"rigctld://127.0.0.1:{rig.port}|monitor:node", 14_090_000.0, 2_500_000, None)
        with mock.patch("pluto_tx.devices.x6200._set_pipewire_volume"):
            dev.prepare_for_start()
        self.addCleanup(dev.close)
        dev.set_power("rfpower", 5.0)
        w = gui.MainWindow("x")
        self.addCleanup(w.close)
        w.tb = mock.Mock(device=dev)
        w.tb.compressor.gain_reduction_db.return_value = 0.0
        w.estop_button.setEnabled(True)
        w.ptt_button.setEnabled(True)
        w.estop_button.click()
        self.assertFalse(w._armed)
        self.assertFalse(w.ptt_button.isEnabled())
        hook = mock.Mock()   # PyQt hands an exception in a slot to sys.excepthook instead of raising it here
        with mock.patch("sys.excepthook", hook):
            w.estop_button.click()
        hook.assert_not_called()
        self.assertTrue(w._armed)
        self.assertTrue(w.ptt_button.isEnabled())
        self.assertEqual(w.estop_button.text(), "E-STOP")

    def test_controls_follow_the_device(self):
        from pluto_tx import gui
        from pluto_tx.flowgraph import PlutoTxFlowgraph as F
        w = gui.MainWindow("x")
        self.addCleanup(w.close)
        w.show()
        w.device_type_combo.setCurrentIndex(w.device_type_combo.findData("x6200"))
        self.app.processEvents()
        self.assertTrue(w.freq_spin.isVisible())
        self.assertTrue(w.power_slider.isVisible())
        self.assertIn("%", w.power_row_label.text())
        self.assertEqual((w.freq_spin.minimum(), w.freq_spin.maximum()), (1.8, 54.0))
        for combo, mode in ((w.digimode_combo, F.MODE_POCSAG), (w.digimode_combo, F.MODE_APRS), (w.mode_combo, F.MODE_FM), (w.mode_combo, F.MODE_M17)):
            self.assertFalse(combo.model().item(combo.findData(mode)).isEnabled(), mode)
        self.assertTrue(w.digimode_combo.model().item(w.digimode_combo.findData(F.MODE_JTTY)).isEnabled())
        self.assertEqual(w._current_mode, F.MODE_SSB)   # was FM, which this radio cannot send
        self.assertIn("CAT", w.device_label.text())
        w.device_type_combo.setCurrentIndex(w.device_type_combo.findData("soundcard"))
        self.app.processEvents()
        self.assertFalse(w.freq_spin.isVisible())


class RxFollowTests(Base):
    def test_tuned_away(self):
        from pluto_advanced_rx.devices.x6200 import X6200RxDevice
        dev = X6200RxDevice(f"rigctld://127.0.0.1:{self.rig.port}|monitor:src", 14_090_000.0, 48_000, None)
        self.addCleanup(dev.close)
        self.assertIsNone(dev.tuned_away_hz())
        with mock.patch("pluto_advanced_rx.devices.x6200._set_pipewire_volume"), \
                mock.patch("pluto_advanced_rx.devices.x6200._AudioToComplexSource"):
            dev.build_source()
        self.assertIsNone(dev.tuned_away_hz())
        self.rig.frequency = 14_100_000.0
        self.assertEqual(dev.tuned_away_hz(), 14_100_000.0)


class RxDeviceTests(Base):
    def make(self, frequency=14_090_000.0):
        from pluto_advanced_rx.devices.x6200 import X6200RxDevice
        dev = X6200RxDevice(f"rigctld://127.0.0.1:{self.rig.port}|monitor:src", frequency, 48_000, None)
        self.addCleanup(dev.close)
        return dev, X6200RxDevice

    def test_tunes_over_cat_and_lowers_the_capture_volume(self):
        from pluto_advanced_rx.devices import x6200 as rx
        dev, cls = self.make()
        self.assertTrue(cls.is_audio_only())
        self.assertTrue(cls.has_frequency_control())
        with mock.patch.object(rx, "_set_pipewire_volume") as volume, mock.patch.object(rx, "_AudioToComplexSource") as source:
            dev.build_source()
        volume.assert_called_once_with("src", rx.CAPTURE_VOLUME)
        source.assert_called_once_with(48_000, "monitor:src")
        self.assertEqual(self.rig.frequency, 14_090_000.0)
        dev.set_frequency(7_078_000.0)
        self.assertEqual(self.rig.frequency, 7_078_000.0)
        self.assertEqual(dev.read_hw_state()["s_meter_db"], -29.0)

    def test_cli_refuses_what_its_audio_cannot_carry(self):
        import io
        import json
        from contextlib import redirect_stdout
        from pluto_cli import app, rx as cli_rx
        for argv, ok in ((["fm"], False), (["ssb", "--digimode", "adsb"], False), (["lsb"], False)):
            out = io.StringIO()
            with redirect_stdout(out), mock.patch.object(cli_rx.runtime, "probe_or_exit"):
                code = app.main(["rx", *argv, "--device", "x6200", "--uri", "/dev/null|x", "--duration", "0.1", "--json"])
            self.assertEqual(code, 1, argv)
            self.assertIn("cannot receive", out.getvalue(), json.dumps(argv))

    def test_frequency_out_of_range_stays_off_the_radio(self):
        dev, _cls = self.make()
        from pluto_advanced_rx.devices import x6200 as rx
        with mock.patch.object(rx, "_set_pipewire_volume"), mock.patch.object(rx, "_AudioToComplexSource"):
            dev.build_source()
        before = self.rig.frequency
        dev.set_frequency(432_150_000.0)
        self.assertEqual(self.rig.frequency, before)

    def test_the_rotator_stays_at_zero_while_the_radio_tunes(self):
        """A plain audio device shifts its band by the frequency; an RF frequency here would lose the signal."""
        import numpy as np
        from gnuradio import blocks, gr
        from pluto_advanced_rx import devices, flowgraph as rxf
        from pluto_advanced_rx.devices import x6200 as rx
        with mock.patch.object(rx, "_set_pipewire_volume"), \
                mock.patch.object(rx, "_AudioToComplexSource", side_effect=lambda *a: blocks.vector_source_c([0j] * 100, True)), \
                mock.patch("pluto_tx.audio_devices.open_output_device", side_effect=lambda *a: blocks.null_sink(gr.sizeof_float)):
            tb = rxf.AdvancedRxFlowgraph(device_type="x6200", uri=f"rigctld://127.0.0.1:{self.rig.port}|monitor:src", frequency=14_090_000.0,
                                         sample_rate=48_000, active_digimode=None)
        self.addCleanup(tb.shutdown)
        self.assertIsNotNone(tb.audio_rotator)
        self.assertEqual(tb._audio_tuning_hz(), 0.0)
        self.assertEqual(self.rig.frequency, 14_090_000.0)
        tb.set_frequency(7_078_000.0)
        self.assertEqual(self.rig.frequency, 7_078_000.0)
        self.assertEqual(tb._audio_tuning_hz(), 0.0)


if __name__ == "__main__":
    unittest.main()
