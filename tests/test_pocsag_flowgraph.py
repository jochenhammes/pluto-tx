"""POCSAG through the real flowgraphs: TX (fake device) -> IQ -> RX (fake device replaying it), plus GUI smoke tests."""
import time
import unittest

import numpy as np

from pluto_tx import config, pocsag, pocsag_codec as codec
from pluto_tx import flowgraph as txf

RATE = 2_500_000
POCSAG = txf.PlutoTxFlowgraph.MODE_POCSAG


def tx_iq(baud, text, ric=1234567, function=3, kind="alpha", charset="ascii"):
    fg = txf.PlutoTxFlowgraph(device_type="fake", mode=POCSAG, pocsag_text=text, pocsag_baud=baud,
                              pocsag_ric=ric, pocsag_function=function, pocsag_kind=kind, pocsag_charset=charset)
    fg.start()
    time.sleep(0.4)
    fg.key_ptt()
    time.sleep(fg.pocsag_hold_s + 0.2)
    fg.unkey_ptt()
    fg.stop()
    fg.wait()
    iq = np.array(fg.device.sink.data(), dtype=np.complex64)
    on = np.flatnonzero(np.abs(iq) > 0.01)
    return iq[on[0]:on[-1]], fg


def rx_messages(iq, offset_hz=0.0, noise_db=None):
    from pluto_advanced_rx import flowgraph as rxf
    from tests import fakes
    n = np.arange(len(iq))
    x = iq * np.exp(2j * np.pi * offset_hz * n / RATE).astype(np.complex64)
    if noise_db is not None:
        sigma = np.sqrt(np.mean(np.abs(x) ** 2) / 10 ** (noise_db / 10) / 2)
        x = x + (sigma * (np.random.randn(len(x)) + 1j * np.random.randn(len(x)))).astype(np.complex64)
    pad = np.zeros(500_000, np.complex64)
    fakes.make_fake_rx(np.concatenate([pad, x, pad]).astype(np.complex64), RATE)
    out = []
    rx = rxf.AdvancedRxFlowgraph(uri="x", frequency=433.45e6, sample_rate=RATE, device_type="fake",
                                 active_digimode="pocsag", on_pocsag_message=out.append)
    rx.start()
    time.sleep((len(x) + 1_000_000) / RATE + 1.0)
    rx.stop()
    rx.wait()
    return out, rx


class TxTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from tests import fakes
        fakes.register_tx()

    def test_deviation_polarity_and_bits(self):
        iq, fg = tx_iq(1200, "DA2JH POCSAG 1200")
        inst = np.angle(iq[1:] * np.conj(iq[:-1])) * RATE / (2 * np.pi)
        self.assertAlmostEqual(float(np.percentile(np.abs(inst), 99.9)), config.POCSAG_DEVIATION_HZ, delta=60)
        n = RATE // 50_000
        dem = inst[: len(inst) // n * n].reshape(-1, n).mean(axis=1)
        out = []
        decoder = codec.BatchDecoder(out.append)
        sps = 50_000 / 1200
        for k in range(int(len(dem) / sps)):
            decoder.push_bit(1 if dem[int((k + 0.5) * sps)] < 0 else 0)   # logic 1 = lower frequency
        decoder.flush()
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["polarity"], 0)                            # standard polarity, no inversion needed
        self.assertEqual((out[0]["ric"], codec.decode_message(out[0])), (1234567, "DA2JH POCSAG 1200"))
        on_air = float((np.abs(iq) > 1e-3).sum()) / RATE
        self.assertAlmostEqual(on_air, fg.pocsag_duration_s, delta=0.06)

    def test_devices_that_swallow_the_burst_end_get_carrier_padding(self):
        from tests import fakes
        fakes.FakeTxDevice.tx_end_loss_s = 0.5
        try:
            iq, fg = tx_iq(2400, "PAD")
            base = txf.PlutoTxFlowgraph(device_type="fake", mode=POCSAG, pocsag_text="PAD", pocsag_baud=2400)
            base.device.tx_end_loss_s = 0.0
            base._ensure_pocsag_audio()
            self.assertAlmostEqual(fg.pocsag_duration_s - base.pocsag_duration_s, 0.5, delta=0.01)
            self.assertAlmostEqual(float((np.abs(iq) > 1e-3).sum()) / RATE, fg.pocsag_duration_s, delta=0.06)
            inst = np.angle(iq[1:] * np.conj(iq[:-1])) * RATE / (2 * np.pi)
            self.assertLess(float(np.abs(inst[-int(0.3 * RATE):]).max()), 50)   # the padding is an unmodulated carrier
        finally:
            fakes.FakeTxDevice.tx_end_loss_s = 0.0

    def test_end_padding_applies_to_the_other_one_shot_digimodes(self):
        from tests import fakes
        durations = {}
        for pad in (0.0, 0.5):
            fakes.FakeTxDevice.tx_end_loss_s = pad
            try:
                fg = txf.PlutoTxFlowgraph(device_type="fake", mode=txf.PlutoTxFlowgraph.MODE_RTTY, rtty_text="TEST",
                                          psk31_text="TEST", digitext_text="TEST")
                fg._ensure_rtty_audio()
                fg._ensure_psk31_audio()
                fg._ensure_digitext_audio()
                durations[pad] = (fg.rtty_duration_s, fg.psk31_duration_s, fg.digitext_duration_s)
            finally:
                fakes.FakeTxDevice.tx_end_loss_s = 0.0
        for plain, padded in zip(durations[0.0], durations[0.5]):
            self.assertAlmostEqual(padded - plain, 0.5, delta=0.02)

    def test_refusals_before_any_rf_action(self):
        fg = txf.PlutoTxFlowgraph(device_type="fake", mode=POCSAG, pocsag_text="")
        self.assertIn("empty", fg.pocsag_problem())
        with self.assertRaises(ValueError):
            fg.key_ptt()
        self.assertFalse(fg.keyed)
        fg.set_pocsag_text("x" * 300)
        self.assertIn("longer", fg.pocsag_problem())
        fg.set_pocsag_text("ok")
        fg.set_pocsag_ric(0)
        fg.set_pocsag_function(0)
        self.assertIn("all-zero", fg.pocsag_problem())
        fg.set_pocsag_ric(codec.RIC_MAX + 1)
        self.assertIn("RIC", fg.pocsag_problem())

    def test_soundcard_keeps_pocsag_and_has_an_audio_output_branch(self):
        fg = txf.PlutoTxFlowgraph(device_type="soundcard", mode=POCSAG, pocsag_text="DA2JH", pocsag_baud=1200)
        self.assertEqual(fg.mode, POCSAG)
        self.assertIsNotNone(fg.pocsag_audio_gain)                            # NRZ audio goes to the sound card
        fg._ensure_pocsag_audio()
        self.assertAlmostEqual(fg.pocsag_duration_s, pocsag.estimate_duration(1234567, 3, "alpha", "DA2JH", 1200), delta=0.01)
        rf = txf.PlutoTxFlowgraph(device_type="fake", mode=POCSAG)
        self.assertIsNone(rf.pocsag_audio_gain)                               # RF devices never open an audio output


class LoopbackTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from tests import fakes
        fakes.register_tx()

    def test_loopback_all_bauds_autobaud_offset_and_noise(self):
        for baud in codec.BAUD_RATES:
            iq, _fg = tx_iq(baud, f"DA2JH POCSAG {baud}", function=3)
            for offset, noise in ((0.0, None), (3000.0, None), (-2500.0, 25.0)):
                with self.subTest(baud=baud, offset=offset, noise=noise):
                    out, rx = rx_messages(iq, offset, noise)
                    self.assertEqual([(m["baud"], m["ric"], m["function"], codec.decode_message(m)) for m in out],
                                     [(baud, 1234567, 3, f"DA2JH POCSAG {baud}")])
                    others = [b for b in rx.pocsag_deframers if b != baud]
                    self.assertTrue(all(rx.pocsag_deframers[b].batches == 0 for b in others))

    def test_numeric_and_german_text(self):
        iq, _ = tx_iq(1200, "0815-4711", ric=42, function=0, kind="numeric")
        out, _rx = rx_messages(iq)
        self.assertEqual([(m["ric"], m["function"], codec.decode_message(m)) for m in out], [(42, 0, "0815-4711")])
        iq, _ = tx_iq(1200, "Grüße", ric=99, function=3, charset="de")
        out, _rx = rx_messages(iq)
        self.assertEqual(codec.decode_message(out[0], "auto", "de"), "Grüße")

    def test_rx_from_the_audio_input(self):
        """A radio's demodulated audio (here: the TX encoder's NRZ stream, any level, with DC offset and noise,
        either polarity) straight into the audio device."""
        from gnuradio import blocks
        from pluto_advanced_rx import devices as rx_devices
        from pluto_advanced_rx import flowgraph as rxf
        from pluto_advanced_rx.devices.base import RxDevice
        for baud in codec.BAUD_RATES:
            for level, sign in ((0.05, 1), (0.8, -1)):
                with self.subTest(baud=baud, level=level, sign=sign):
                    audio, _ = pocsag.encode_message(1234567, 3, "alpha", f"Audio {baud}", baud)
                    x = sign * level * audio + 0.02 * level * np.random.randn(len(audio)) + 0.1 * level
                    x = np.concatenate([np.zeros(24000), x, np.zeros(24000)]).astype(np.float32)

                    class FakeAudio(RxDevice):
                        device_type = "fake"
                        display_name = "fake audio"
                        connection_kind = "audio_device"
                        DEFAULT_CONNECTION = ""
                        frequency_range_hz = (0.0, 0.0)
                        audio_tuning_range_hz = (0.0, 24000.0)
                        sample_rate_hz_choices = (48000,)
                        default_sample_rate_hz = 48000
                        default_bandwidth_hz = None
                        gain_stages = ()
                        supports_agc_mode = False
                        agc_modes = ()
                        default_gain_mode = None
                        max_waterfall_zoom = 8
                        supports_dc_iq_correction = False

                        def build_source(self):
                            return blocks.vector_source_c((x.astype(np.complex64)).tolist(), False)

                        def set_frequency(self, hz): pass
                        def set_gain(self, name, value): pass
                        def read_hw_state(self): return {}
                        @staticmethod
                        def probe_with_timeout(connection, timeout_s=5): return None
                        @staticmethod
                        def scan_devices_with_timeout(timeout_s=5): return {}, None

                    rx_devices.DEVICE_REGISTRY["fake"] = FakeAudio
                    out = []
                    rx = rxf.AdvancedRxFlowgraph(uri="", sample_rate=48000, device_type="fake", active_digimode="pocsag",
                                                 on_pocsag_message=out.append)
                    rx.start()
                    time.sleep(len(x) / 48000 * 0.7 + 3)
                    rx.stop()
                    rx.wait()
                    self.assertEqual([(m["baud"], m["ric"], codec.decode_message(m)) for m in out],
                                     [(baud, 1234567, f"Audio {baud}")])


class GuiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PyQt5 import QtWidgets
        from tests import fakes
        fakes.register_tx()
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def test_tx_gui_group_and_settings(self):
        from pluto_tx import gui
        w = gui.MainWindow("x")
        w.digimode_combo.setCurrentIndex(w.digimode_combo.findData(POCSAG))
        w.mode_tab_widget.setCurrentIndex(1)
        self.assertEqual(w._current_mode, POCSAG)
        self.assertTrue(w.pocsag_group_widget.isVisibleTo(w))
        self.assertIn("Enter a message", w.pocsag_info_label.text())
        w.pocsag_text_edit.setText("DA2JH test")
        self.assertIn("1.39 s", w.pocsag_info_label.text())
        w.pocsag_kind_combo.setCurrentIndex(w.pocsag_kind_combo.findData("numeric"))
        self.assertEqual(w.pocsag_function_combo.currentData(), 0)
        tb = txf.PlutoTxFlowgraph(device_type="fake", mode=POCSAG)
        w.pocsag_ric_spin.setValue(4242)
        w.pocsag_baud_combo.setCurrentIndex(w.pocsag_baud_combo.findData(2400))
        w._apply_pocsag(tb)                                                # what _rebuild() does
        self.assertEqual((tb.pocsag_ric, tb.pocsag_kind, tb.pocsag_baud, tb.pocsag_text),
                         (4242, "numeric", 2400, "DA2JH test"))

    def test_rx_gui_table_and_rerender(self):
        from pluto_advanced_rx import gui
        w = gui.MainWindow("x")
        w.digimode_combo.setCurrentIndex(w.digimode_combo.findData("pocsag"))
        bits = codec.text_to_bits("Grüße", "alpha", "de")
        w._pocsag_state.on_message(dict(time=0, baud=1200, ric=77, function=3, payload="".join(map(str, bits)),
                                        corrected=1, uncorrectable=0, polarity=1))
        w._render_pocsag_table()
        self.assertEqual(w.pocsag_table.item(0, 2).text(), "77")
        self.assertEqual(w.pocsag_table.item(0, 4).text(), "Gr}~e")   # DIN 66003 bytes seen as plain ASCII
        w.pocsag_charset_checkbox.setChecked(True)
        w._render_pocsag_table()
        self.assertEqual(w.pocsag_table.item(0, 4).text(), "Grüße")
        w._pocsag_state.on_message(dict(time=0, baud=512, ric=5, function=3, payload="0" * 20,
                                        corrected=0, uncorrectable=2, polarity=0))
        self.assertEqual(len(w._pocsag_state.get_snapshot()[1]), 1)        # damaged calls hidden by default
        w.pocsag_hide_damaged_checkbox.setChecked(False)
        self.assertEqual(len(w._pocsag_state.get_snapshot()[1]), 2)
        w.pocsag_interp_combo.setCurrentIndex(w.pocsag_interp_combo.findData("numeric"))
        w._render_pocsag_table()
        self.assertNotEqual(w.pocsag_table.item(0, 4).text(), "Grüße")


if __name__ == "__main__":
    unittest.main()
