"""The X6200 device, its CAT client and its PTT guard, against a fake rigctld with the measured behaviour of the real radio."""
import os
import subprocess
import sys
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
        for mode in ("jtty", "ft8", "js8", "rtty", "psk31", "digitext", "rade"):
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

    def test_power_is_written_only_when_it_differs(self):
        dev = self.make()
        self.rig.log.clear()
        dev.set_power("rfpower", 5.0)
        self.assertEqual(self.rig.commands("L"), [])

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

    def test_an_unsupported_mode_does_not_fall_back_to_fm(self):
        from pluto_tx import flowgraph as txf
        fg = self.make(txf.PlutoTxFlowgraph.MODE_FM)
        self.assertEqual(fg.mode, txf.PlutoTxFlowgraph.MODE_RTTY)


if __name__ == "__main__":
    unittest.main()
