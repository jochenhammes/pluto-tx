"""ADS-L through the real RX flowgraph (GNU Radio blocks, fake device replaying synthetic IQ)."""
import contextlib
import io
import json
import os
import signal
import sys
import unittest
from unittest import mock

import numpy as np

from pluto_advanced_rx import adsl_state, config
from pluto_advanced_rx.adsl_state import AdslState
from pluto_tx import adsl_codec as c
from tests import adsl_testgen as gen


def scene(rate, packets, noise_snr_db=None, offset_hz=0.0, lo_hz=config.ADSL_LO_HZ, seed=1, pad_s=0.05):
    """packets: list of (channel_hz, start_s, address). All bursts unit amplitude, on their real offset from the LO."""
    rng = np.random.default_rng(seed)
    total = int((max(p[1] for p in packets) + 0.02 + pad_s) * rate)
    x = np.zeros(total, np.complex128)
    for channel_hz, start_s, address in packets:
        burst = gen.modulate(gen.air_chips(gen.frame(gen.traffic_clear(address=address))), rate)
        start = int((start_s + pad_s) * rate)
        t = np.arange(start, start + len(burst))
        x[start:start + len(burst)] += burst * np.exp(2j * np.pi * (channel_hz - lo_hz + offset_hz) * t / rate)
    if noise_snr_db is not None:
        sigma = np.sqrt(10 ** (-noise_snr_db / 10) * rate / 200_000 / 2)
        x += sigma * (rng.standard_normal(total) + 1j * rng.standard_normal(total))
    return x.astype(np.complex64)


def run(iq, rate, lo_hz=config.ADSL_LO_HZ, **kw):
    from pluto_advanced_rx import flowgraph as rxf
    from tests import fakes
    fakes.make_fake_rx(iq, rate)
    out = []
    rx = rxf.AdvancedRxFlowgraph(uri="x", frequency=lo_hz, sample_rate=rate, device_type="fake",
                                 active_digimode="adsl", on_adsl_packet=out.append, **kw)
    rx.start()
    rx.wait()
    for _channel, _xlate, _demod, deframer in rx.adsl_channels:
        deframer._detector.flush()
    return out, rx


class FlowgraphTests(unittest.TestCase):
    def test_both_channels_at_every_device_rate(self):
        for rate in (2_000_000, 2_048_000, 2_400_000, 2_500_000, 4_000_000):
            with self.subTest(rate=rate):
                iq = scene(rate, [(868_200_000, 0.0, 0x111111), (868_400_000, 0.05, 0x222222)], noise_snr_db=25)
                out, _rx = run(iq, rate)
                self.assertEqual({(p["channel_hz"], p["address"]) for p in out},
                                 {(868_200_000, 0x111111), (868_400_000, 0x222222)})

    def test_simultaneous_packets_on_both_channels(self):
        iq = scene(2_400_000, [(868_200_000, 0.0, 0x111111), (868_400_000, 0.0, 0x222222)], noise_snr_db=25)
        out, _rx = run(iq, 2_400_000)
        self.assertEqual({(p["channel_hz"], p["address"]) for p in out},
                         {(868_200_000, 0x111111), (868_400_000, 0x222222)})

    def test_frequency_error_is_reported(self):
        iq = scene(2_400_000, [(868_200_000, 0.0, 0x111111)], noise_snr_db=25, offset_hz=30_000)
        out, _rx = run(iq, 2_400_000)
        self.assertEqual(len(out), 1)
        self.assertAlmostEqual(out[0]["frequency_offset_hz"], 30_000, delta=3000)

    def test_sensitivity_through_gnu_radio_blocks(self):
        rate = 2_400_000
        packets = [(config.ADSL_CHANNELS_HZ[i % 2], 0.01 * i, 0x100000 + i) for i in range(60)]
        out, _rx = run(scene(rate, packets, noise_snr_db=12), rate)
        self.assertGreaterEqual(len({p["address"] for p in out}), 58)

    def test_retune_follows_the_channels(self):
        rate = 2_400_000
        iq = scene(rate, [(868_200_000, 0.0, 0x111111)], noise_snr_db=25, lo_hz=868_860_000)
        from pluto_advanced_rx import flowgraph as rxf
        from tests import fakes
        fakes.make_fake_rx(iq, rate)
        out = []
        rx = rxf.AdvancedRxFlowgraph(uri="x", frequency=config.ADSL_LO_HZ, sample_rate=rate, device_type="fake",
                                     active_digimode="adsl", on_adsl_packet=out.append)
        rx.set_frequency(868_860_000)
        rx.start()
        rx.wait()
        for _channel, _xlate, _demod, deframer in rx.adsl_channels:
            deframer._detector.flush()
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["channel_hz"], 868_200_000)
        self.assertLess(abs(out[0]["frequency_offset_hz"]), 3000)

    def test_same_frame_on_both_channels_counts_once(self):
        """A strong sender also decodes on the neighbour channel (seen on air at close range). The stand-in here is
        the same frame transmitted on both, which is what the receiver sees afterwards."""
        iq = scene(2_400_000, [(868_200_000, 0.0, 0x111111), (868_400_000, 0.0, 0x111111), (868_200_000, 0.05, 0x222222)],
                   noise_snr_db=30)
        out, _rx = run(iq, 2_400_000)
        self.assertEqual(sorted(p["address"] for p in out), [0x111111, 0x222222])

    def test_too_low_sample_rate_is_refused(self):
        from pluto_advanced_rx import flowgraph as rxf
        from tests import fakes
        fakes.make_fake_rx(np.zeros(100, np.complex64), 1_000_000)
        with self.assertRaisesRegex(ValueError, "too low for ADS-L"):
            rxf.AdvancedRxFlowgraph(uri="x", frequency=config.ADSL_LO_HZ, sample_rate=1_000_000, device_type="fake",
                                    active_digimode="adsl")

    def test_other_digimodes_do_not_wire_adsl(self):
        from pluto_advanced_rx import flowgraph as rxf
        from tests import fakes
        fakes.make_fake_rx(np.zeros(100, np.complex64), 2_500_000)
        rx = rxf.AdvancedRxFlowgraph(uri="x", frequency=433e6, sample_rate=2_500_000, device_type="fake")
        self.assertEqual(rx.adsl_channels, [])


    def test_tuning_away_is_refused_at_build_and_detected_later(self):
        from pluto_advanced_rx import flowgraph as rxf
        from tests import fakes
        fakes.make_fake_rx(np.zeros(100, np.complex64), 2_400_000)
        with self.assertRaisesRegex(ValueError, "outside the tuned span"):
            rxf.AdvancedRxFlowgraph(uri="x", frequency=433e6, sample_rate=2_400_000, device_type="fake", active_digimode="adsl")
        rx = rxf.AdvancedRxFlowgraph(uri="x", frequency=config.ADSL_LO_HZ, sample_rate=2_400_000, device_type="fake",
                                     active_digimode="adsl")
        self.assertTrue(rx.adsl_all_channels_in_span())
        rx.set_frequency(870e6)
        self.assertFalse(rx.adsl_all_channels_in_span())


def run_cli(argv):
    from pluto_cli import app
    out = io.StringIO()
    saved = (signal.getsignal(signal.SIGINT), signal.getsignal(signal.SIGTERM), sys.excepthook)
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            try:
                code = app.main(argv)
            except SystemExit as e:
                code = e.code
    finally:
        signal.signal(signal.SIGINT, saved[0])
        signal.signal(signal.SIGTERM, saved[1])
        sys.excepthook = saved[2]
    events = []
    for line in out.getvalue().splitlines():
        try:
            events.append(json.loads(line))
        except ValueError:
            pass
    return code, events


class CliTests(unittest.TestCase):
    def test_packets_as_json_events(self):
        from tests import fakes
        rate = 2_500_000
        iq = scene(rate, [(868_200_000, 0.0, 0x111111), (868_400_000, 0.05, 0x222222)], noise_snr_db=25, pad_s=0.3)
        fakes.make_fake_rx(np.concatenate([iq, np.zeros(rate // 2, np.complex64)]), rate)
        code, events = run_cli(["rx", "fm", "--device", "fake", "--uri", "x", "--freq", "433000000", "--digimode", "adsl",
                                "--duration", "2", "--json"])
        self.assertEqual(code, 0)
        listen = next(e for e in events if e["event"] == "adsl_listen")
        self.assertEqual(listen["freq_hz"], config.ADSL_LO_HZ)
        packets = [e for e in events if e["event"] == "adsl_packet"]
        self.assertEqual({(p["channel_hz"], p["address"]) for p in packets},
                         {(868_200_000, "111111"), (868_400_000, "222222")})
        self.assertEqual(packets[0]["address_label"][:6], "FANET:")
        self.assertEqual(packets[0]["alt_m"], 1234)

    def test_audio_stays_muted(self):
        from pluto_advanced_rx import flowgraph as rxf
        from tests import fakes
        fakes.make_fake_rx(np.zeros(100_000, np.complex64), 2_500_000)
        with mock.patch.object(rxf.AdvancedRxFlowgraph, "set_rx_muted") as muted:
            run_cli(["rx", "fm", "--device", "fake", "--uri", "x", "--digimode", "adsl", "--duration", "0.2", "--json"])
        self.assertEqual(muted.call_args_list[0], mock.call(True))
        self.assertNotIn(mock.call(False), muted.call_args_list)

    def test_bandwidth_is_checked_per_device(self):
        from tests import fakes
        fakes.make_fake_rx(np.zeros(100, np.complex64), 2_500_000)
        code, events = run_cli(["rx", "fm", "--device", "fake", "--uri", "x", "--bandwidth", "2400000", "--json"])
        self.assertEqual(code, 1)
        self.assertIn("not available on --device fake", json.dumps(events))

    def test_too_low_rate_is_refused(self):
        from tests import fakes
        fakes.make_fake_rx(np.zeros(100, np.complex64), 1_000_000)
        code, events = run_cli(["rx", "fm", "--device", "fake", "--uri", "x", "--bandwidth", "1000000", "--digimode", "adsl",
                                "--json"])
        self.assertEqual(code, 1)
        self.assertIn("needs at least", json.dumps(events))


class StateTests(unittest.TestCase):
    def packet(self, **kw):
        info = c.parse_frame(gen.frame(gen.traffic_clear(**kw)))
        info.update(channel_hz=868_200_000, frequency_offset_hz=1000)
        return info

    def test_one_aircraft_several_packets(self):
        state = AdslState()
        state.on_packet(self.packet(alt_m=100), now=1.0)
        state.on_packet(self.packet(alt_m=200), now=2.0)
        state.on_packet(self.packet(address=5, alt_m=300), now=1.5)
        aircraft = state.get_aircraft()
        self.assertEqual([a["label"] for a in aircraft], ["FANET:123456", "FANET:000005"])
        self.assertEqual(aircraft[0]["packets"], 2)
        self.assertEqual(aircraft[0]["alt_m"], 200)

    def test_status_packet_keeps_the_last_position(self):
        state = AdslState()
        state.on_packet(self.packet(), now=1.0)
        status = c.parse_frame(gen.frame(bytes((3,)) + gen.traffic_clear()[1:]))
        status.update(channel_hz=868_400_000, frequency_offset_hz=0)
        state.on_packet(status, now=2.0)
        aircraft = state.get_aircraft()[0]
        self.assertAlmostEqual(aircraft["lat"], 48.12345, places=4)
        self.assertEqual(aircraft["packets"], 2)
        self.assertEqual(aircraft["kinds"], {"traffic", "status"})

    def test_lost_fix_clears_the_old_position(self):
        state = AdslState()
        state.on_packet(self.packet(), now=1.0)
        lost = self.packet()
        lost.update(lat=None, lon=None)
        state.on_packet(lost, now=2.0)
        self.assertIsNone(state.get_aircraft()[0]["lat"])

    def test_limits_and_clear(self):
        state = AdslState()
        for i in range(adsl_state.MAX_ROWS + 10):
            state.on_packet(self.packet(address=i % (adsl_state.MAX_AIRCRAFT + 5)), now=float(i))
        self.assertEqual(state.counts(), (adsl_state.MAX_ROWS, adsl_state.MAX_AIRCRAFT))
        state.clear()
        self.assertEqual(state.counts(), (0, 0))

    def test_encrypted_packet_is_logged_without_aircraft(self):
        state = AdslState()
        body = bytes((1 << 5,)) + bytes(20)
        state.on_packet(c.parse_frame(bytes((len(body) + 3,)) + body + c.crc24(body).to_bytes(3, "big")), now=1.0)
        self.assertEqual(state.counts(), (1, 0))


class GuiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt5 import QtWidgets
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def test_tables_and_emergency_highlight(self):
        from pluto_advanced_rx import gui
        w = gui.MainWindow("x")
        w.digimode_combo.setCurrentIndex(w.digimode_combo.findData("adsl"))
        self.assertFalse(w.adsl_group_widget.isHidden())
        self.assertTrue(w.pocsag_group_widget.isHidden())
        info = c.parse_frame(gen.frame(gen.traffic_clear(emergency=2, climb_mps=2.0, speed_mps=25.0)))
        info.update(channel_hz=868_400_000, frequency_offset_hz=-1500)
        w._adsl_state.on_packet(info)
        w._poll_digimode()
        columns = w.ADSL_AIRCRAFT_COLUMNS
        table = w.adsl_aircraft_table
        cell = lambda name: table.item(0, columns.index(name)).text()
        self.assertEqual(cell("Address"), "FANET:123456")
        self.assertEqual(cell("Category"), "glider")
        self.assertEqual(cell("Latitude"), "48.12345")
        self.assertEqual(cell("Alt m"), "1234")
        self.assertEqual(cell("km/h"), "90")
        self.assertEqual(cell("Climb m/s"), "+2.0")
        self.assertEqual(cell("MHz"), "868.4")
        self.assertEqual(cell("Offset kHz"), "-1.5")
        self.assertEqual(cell("Emergency"), "general")
        self.assertEqual(w.adsl_table.item(0, 2).text(), "FANET:123456")
        self.assertIn("48.12345, 8.12345", w.adsl_table.item(0, 3).text())
        w.adsl_clear_button.click()
        self.assertEqual(table.rowCount(), 0)
        self.assertEqual(w.adsl_table.rowCount(), 0)

    def window_on_fake_device(self, rate=2_400_000):
        from pluto_advanced_rx import flowgraph as rxf, gui
        from tests import fakes
        fakes.make_fake_rx(np.zeros(4096, np.complex64), rate)
        w = gui.MainWindow("x")
        w.device_type_combo.blockSignals(True)
        w.device_type_combo.addItem("fake", "fake")
        w.device_type_combo.setCurrentIndex(w.device_type_combo.findData("fake"))
        w.device_type_combo.blockSignals(False)
        w.tb = rxf.AdvancedRxFlowgraph(uri="x", frequency=433.5e6, sample_rate=rate, device_type="fake")
        w.freq_spin.setValue(433.5)
        return w

    def test_entering_the_mode_tunes_to_the_lo_and_leaving_restores(self):
        w = self.window_on_fake_device()
        w._rebuild_for_digimode("adsl")
        self.assertEqual(w.tb.active_digimode, "adsl")
        self.assertEqual(w.tb.nominal_freq_hz, config.ADSL_LO_HZ)
        self.assertAlmostEqual(w.freq_spin.value(), 868.85)
        self.assertEqual(len(w.tb.adsl_channels), 2)
        self.assertIsNone(w.tb.filebroadcast_deframer)
        w._update_adsl_signal_label()
        self.assertIn("868.2 + 868.4 MHz", w.adsl_signal_label.text())
        w._poll_digimode()  # includes the file transfer poll, whose deframer is absent in this mode
        w._rebuild_for_digimode(None)
        self.assertEqual(w.tb.nominal_freq_hz, 433.5e6)
        self.assertAlmostEqual(w.freq_spin.value(), 433.5)
        self.assertIsNotNone(w.tb.filebroadcast_deframer)
        w.tb.shutdown()

    def test_too_low_rate_reports_and_keeps_the_old_flowgraph(self):
        w = self.window_on_fake_device(rate=1_000_000)
        old = w.tb
        w._rebuild_for_digimode("adsl")
        self.assertIs(w.tb, old)
        self.assertIn("too low for ADS-L", w.status_label.text())
        self.assertIsNone(w._active_digimode)

    def test_tuning_away_shows_a_warning(self):
        w = self.window_on_fake_device()
        w._rebuild_for_digimode("adsl")
        w.tb.set_frequency(870e6)
        w._update_adsl_signal_label()
        self.assertIn("Tuned away", w.adsl_signal_label.text())
        w.tb.shutdown()

    def test_untouched_rtl_gain_is_raised_and_restored(self):
        from pluto_advanced_rx import gui
        w = gui.MainWindow("x")
        w.device_type_combo.setCurrentIndex(w.device_type_combo.findData("rtlsdr"))
        self.assertEqual(w.gain_slider.value(), 20)
        w._apply_adsl_gain("adsl")
        self.assertEqual(w.gain_slider.value(), int(config.ADSL_RTL_TUNER_GAIN_DB))
        self.assertEqual(w.gain_label.text(), "40 dB")
        self.assertEqual(w._current_gain_kwargs(gui.devices.DEVICE_REGISTRY["rtlsdr"])["manual_gain_db"], 40.0)
        w._apply_adsl_gain(None)
        self.assertEqual(w.gain_slider.value(), 20)

    def test_a_gain_the_operator_chose_stays(self):
        from pluto_advanced_rx import gui
        w = gui.MainWindow("x")
        w.device_type_combo.setCurrentIndex(w.device_type_combo.findData("rtlsdr"))
        w.gain_slider.setValue(33)
        w._apply_adsl_gain("adsl")
        self.assertEqual(w.gain_slider.value(), 33)
        w._apply_adsl_gain(None)
        self.assertEqual(w.gain_slider.value(), 33)
        w.gain_slider.setValue(20)
        w._apply_adsl_gain("adsl")
        w.gain_slider.setValue(38)  # changed while in the mode: no longer ours to undo
        w._apply_adsl_gain(None)
        self.assertEqual(w.gain_slider.value(), 38)

    def test_other_devices_keep_their_gain(self):
        from pluto_advanced_rx import gui
        w = gui.MainWindow("x")
        before = w.gain_slider.value()
        w._apply_adsl_gain("adsl")
        self.assertEqual(w.gain_slider.value(), before)

    def test_soundcard_cannot_select_adsl(self):
        from pluto_advanced_rx import gui
        w = gui.MainWindow("x")
        w.digimode_combo.setCurrentIndex(w.digimode_combo.findData("adsl"))
        w.device_type_combo.setCurrentIndex(w.device_type_combo.findData("audio"))
        self.assertNotEqual(w.digimode_combo.currentData(), "adsl")
        item = w.digimode_combo.model().item(w.digimode_combo.findData("adsl"))
        self.assertFalse(item.isEnabled())


if __name__ == "__main__":
    unittest.main()
