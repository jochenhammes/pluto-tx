"""FM sub-audible tones (CTCSS/DCS) and USB/LSB in the TX flowgraph, checked on the captured IQ of
the fake device, plus the pure DCS word/loop maths and the LSB demodulator of the RX flowgraph."""
import os
import tempfile
import time
import unittest
import wave

import numpy as np
from scipy import signal

from pluto_advanced_rx import config as config_rx
from pluto_tx import config, dcs
from pluto_tx import flowgraph as txf

RATE = 2_500_000
FM, SSB, LSB = txf.PlutoTxFlowgraph.MODE_FM, txf.PlutoTxFlowgraph.MODE_SSB, txf.PlutoTxFlowgraph.MODE_LSB


def write_wav(path, samples):
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(config.AUDIO_RATE)
        w.writeframes((np.clip(samples, -1, 1) * 32767).astype("<i2").tobytes())


def fm_demod(iq, out_rate=50_000):
    inst = np.angle(iq[1:] * np.conj(iq[:-1])) * RATE / (2 * np.pi)
    n = RATE // out_rate
    inst = inst[: len(inst) // n * n]
    return inst.reshape(-1, n).mean(axis=1)


def line_amplitude(x, fs, f):
    x = x - np.mean(x)
    w = np.hanning(len(x))
    spec = np.abs(np.fft.rfft(x * w)) * 2 / w.sum()
    return float(spec[int(round(f * len(x) / fs))])


class DcsPureTests(unittest.TestCase):
    def test_d023_matches_published_example(self):
        # published: 023 is sent as 110010000-001-11000110111 (code bits LSB first, marker, check bits)
        b = "".join(map(str, dcs.word_bits(0o23)))
        self.assertEqual((b[:9], b[9:12], b[12:]), ("110010000", "001", "11000110111"))

    def test_code_lists(self):
        self.assertEqual(len(dcs.STANDARD_CODES), 83)
        self.assertEqual(len(set(dcs.STANDARD_CODES)), 83)
        self.assertEqual(len(config.CTCSS_TONES_HZ), 50)
        words = {c: dcs.golay_word(c) for c in dcs.STANDARD_CODES}
        for a in words.values():
            self.assertLess(a, 1 << 23)
        cs = list(words.values())
        self.assertGreaterEqual(min(bin(cs[i] ^ cs[j]).count("1")
                                    for i in range(len(cs)) for j in range(i)), 7)  # Golay distance

    def test_parse_and_polarity(self):
        self.assertEqual(dcs.parse_code("023"), (0o23, "N"))
        self.assertEqual(dcs.parse_code("d754i"), (0o754, "I"))
        with self.assertRaises(ValueError):
            dcs.parse_code("024")
        n, i = dcs.render_loop(0o23), dcs.render_loop(0o23, "I")
        np.testing.assert_allclose(n, -i)
        self.assertAlmostEqual(float(np.abs(n).max()), 1.0, places=5)

    def test_loop_is_seamless(self):
        loop = dcs.render_loop(0o125)
        self.assertLess(abs(float(loop[0] - loop[-1])), 0.02)   # step across the seam ~ a normal sample step
        self.assertLess(float(np.abs(np.diff(loop)).max()), 0.03)


class TxTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from tests import fakes
        fakes.register_tx()
        cls.tmp = tempfile.mkdtemp()
        t = np.arange(config.AUDIO_RATE * 4) / config.AUDIO_RATE
        cls.silence = os.path.join(cls.tmp, "silence.wav")
        cls.tone = os.path.join(cls.tmp, "tone.wav")
        write_wav(cls.silence, np.zeros(len(t)))
        write_wav(cls.tone, 0.6 * np.sin(2 * np.pi * 1000 * t))

    def capture(self, wav, mode=FM, setup=None, seconds=2.2, after_start=None):
        fg = txf.PlutoTxFlowgraph(device_type="fake", mode=mode, source=txf.PlutoTxFlowgraph.SRC_FILE,
                                  wav_path=wav)
        if setup:
            setup(fg)
        fg.start()
        if after_start:
            after_start(fg)
        time.sleep(0.5)
        fg.key_ptt()
        time.sleep(seconds)
        fg.unkey_ptt()
        fg.stop()
        fg.wait()
        iq = np.array(fg.device.sink.data(), dtype=np.complex64)
        on = np.flatnonzero(np.abs(iq) > 0.01)
        self.assertGreater(len(on), RATE)
        return iq[on[0] + int(0.5 * RATE):on[0] + int((seconds - 0.2) * RATE)], fg

    def test_ctcss_line_and_deviation(self):
        iq, _ = self.capture(self.silence, setup=lambda fg: fg.set_subtone("ctcss", 88.5))
        dem = fm_demod(iq)
        self.assertAlmostEqual(line_amplitude(dem, 50_000, 88.5), 0.12 * config.FM_DEVIATION_HZ, delta=25)
        self.assertLess(line_amplitude(dem, 50_000, 100.0), 10)

    def test_subtone_off_is_plain_fm_and_live_switch(self):
        def setup(fg):
            fg.set_subtone("ctcss", 100.0)
            fg.set_subtone("off")
        iq, _ = self.capture(self.silence, setup=setup)
        self.assertLess(float(np.abs(fm_demod(iq)).max()), 5)
        iq, _ = self.capture(self.silence, setup=lambda fg: (fg.set_subtone("ctcss", 88.5),
                                                              fg.set_subtone("ctcss", 123.0)))
        dem = fm_demod(iq)
        self.assertGreater(line_amplitude(dem, 50_000, 123.0), 250)
        self.assertLess(line_amplitude(dem, 50_000, 88.5), 10)

    def test_voice_plus_tone_stays_within_deviation(self):
        iq, _ = self.capture(self.tone, setup=lambda fg: fg.set_subtone("ctcss", 88.5))
        dem = fm_demod(iq)
        self.assertLess(float(np.abs(dem).max()), config.FM_DEVIATION_HZ * 1.03)
        self.assertGreater(line_amplitude(dem, 50_000, 1000.0), 300)      # voice still there
        self.assertAlmostEqual(line_amplitude(dem, 50_000, 88.5), 0.12 * config.FM_DEVIATION_HZ, delta=30)

    def test_level_setting(self):
        def setup(fg):
            fg.set_subtone("ctcss", 88.5)
            fg.set_subtone_level(20)
        iq, _ = self.capture(self.silence, setup=setup)
        self.assertAlmostEqual(line_amplitude(fm_demod(iq), 50_000, 88.5), 0.20 * config.FM_DEVIATION_HZ, delta=30)

    def test_dcs_waveform_matches_reference_and_polarity(self):
        for polarity, sign in (("N", 1), ("I", -1)):
            with self.subTest(polarity=polarity):
                iq, _ = self.capture(self.silence, setup=lambda fg: fg.set_subtone("dcs", (0o23, polarity)), seconds=3.2)
                dem = fm_demod(iq)
                ref = signal.resample_poly(np.tile(dcs.render_loop(0o23), 2), 25, 24)
                dem = dem - np.mean(dem)
                corr = signal.correlate(dem, ref - np.mean(ref), mode="valid", method="fft")
                norm = np.linalg.norm(ref - np.mean(ref)) * np.sqrt(np.mean(dem ** 2) * len(ref))
                peak = corr[np.argmax(np.abs(corr))] / norm
                self.assertGreater(sign * peak, 0.9)
                self.assertAlmostEqual(float(np.abs(dem).max()), 0.12 * config.FM_DEVIATION_HZ, delta=40)

    def test_invalid_subtone_rejected(self):
        fg = txf.PlutoTxFlowgraph(device_type="fake", mode=FM, source=txf.PlutoTxFlowgraph.SRC_FILE, wav_path=self.tone)
        with self.assertRaises(ValueError):
            fg.set_subtone("dcs", (0o24, "N"))
        with self.assertRaises(ValueError):
            fg.set_subtone("rtty")

    def test_usb_lsb_sidebands(self):
        for mode, want in ((SSB, 1), (LSB, -1)):
            with self.subTest(mode=mode):
                iq, fg = self.capture(self.tone, mode=mode)
                self.assertEqual(fg.mode, mode)
                spec = np.abs(np.fft.fft(iq[:2 ** 21])) ** 2
                freqs = np.fft.fftfreq(2 ** 21, 1 / RATE)
                wanted = spec[np.abs(freqs - want * 1000) < 60].sum()
                mirror = spec[np.abs(freqs + want * 1000) < 60].sum()
                self.assertGreater(10 * np.log10(wanted / mirror), 40)

    def test_live_switch_ssb_to_lsb(self):
        iq, fg = self.capture(self.tone, mode=SSB, after_start=lambda fg: fg.set_mode(LSB))
        spec = np.abs(np.fft.fft(iq[:2 ** 21])) ** 2
        freqs = np.fft.fftfreq(2 ** 21, 1 / RATE)
        self.assertGreater(spec[np.abs(freqs + 1000) < 60].sum(), 1e4 * spec[np.abs(freqs - 1000) < 60].sum())


class RxLsbTests(unittest.TestCase):
    def run_rx(self, iq, mode, rate=1_000_000):
        from pluto_advanced_rx import flowgraph as rxf
        from tests import fakes
        fakes.make_fake_rx(iq, rate)
        rx = rxf.AdvancedRxFlowgraph(uri="x", frequency=14_200_000.0, sample_rate=rate, device_type="fake",
                                     demod_mode=mode)
        return rx, rxf

    def test_selectable_modes_and_filter_sides(self):
        from pluto_advanced_rx import flowgraph as rxf
        self.assertTrue(hasattr(rxf.AdvancedRxFlowgraph, "MODE_LSB"))
        rate = 1_000_000
        t = np.arange(rate * 3) / rate
        results = {}
        for signal_side in (1, -1):
            iq = (0.5 * np.exp(2j * np.pi * signal_side * 1000 * t)).astype(np.complex64)   # tone at +-1 kHz
            for mode in (rxf.AdvancedRxFlowgraph.MODE_SSB, rxf.AdvancedRxFlowgraph.MODE_LSB):
                rx, _ = self.run_rx(iq, mode, rate)
                from gnuradio import blocks
                sink = blocks.vector_sink_f()
                rx.connect(rx.demod_selector, sink)
                rx.start()
                time.sleep(2.5)
                rx.stop()
                rx.wait()
                audio = np.array(sink.data(), dtype=np.float32)
                results[(signal_side, mode)] = float(np.sqrt(np.mean(audio[len(audio) // 2:] ** 2))) if len(audio) else 0.0
        usb, lsb = rxf.AdvancedRxFlowgraph.MODE_SSB, rxf.AdvancedRxFlowgraph.MODE_LSB
        self.assertGreater(results[(1, usb)], 30 * results[(-1, usb)] + 1e-6)
        self.assertGreater(results[(-1, lsb)], 30 * results[(1, lsb)] + 1e-6)


class GuiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PyQt5 import QtWidgets
        from tests import fakes
        fakes.register_tx()
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def test_tx_subtone_row(self):
        from pluto_tx import gui
        w = gui.MainWindow("x")
        self.assertTrue(w.subtone_row_widget.isVisibleTo(w))               # FM is the default mode
        self.assertEqual(w.subtone_kind_combo.currentData(), "off")
        self.assertFalse(w.subtone_value_combo.isEnabled() and w.subtone_kind_combo.currentData() == "off")
        w.subtone_kind_combo.setCurrentIndex(w.subtone_kind_combo.findData("ctcss"))
        self.assertEqual(w.subtone_value_combo.count(), 50)
        self.assertEqual(w.subtone_value_combo.currentData(), 88.5)
        w.subtone_kind_combo.setCurrentIndex(w.subtone_kind_combo.findData("dcs"))
        self.assertEqual(w.subtone_value_combo.count(), 166)
        self.assertEqual(w.subtone_value_combo.currentData(), (0o23, "N"))
        w.mode_combo.setCurrentIndex(w.mode_combo.findData(LSB))
        self.assertFalse(w.subtone_row_widget.isVisibleTo(w))
        w.mode_combo.setCurrentIndex(w.mode_combo.findData(FM))
        self.assertTrue(w.subtone_row_widget.isVisibleTo(w))

    def test_tx_subtone_reapplied_to_new_flowgraph(self):
        from pluto_tx import gui
        w = gui.MainWindow("x")
        w.subtone_kind_combo.setCurrentIndex(w.subtone_kind_combo.findData("ctcss"))
        w.subtone_value_combo.setCurrentIndex(w.subtone_value_combo.findData(123.0))
        w.subtone_level_spin.setValue(15)
        tb = txf.PlutoTxFlowgraph(device_type="fake", mode=FM, source=txf.PlutoTxFlowgraph.SRC_FILE)
        w._apply_subtone(tb)                                               # what _rebuild() does
        self.assertEqual((tb._subtone_kind, tb._subtone_value, tb._subtone_level_pct), ("ctcss", 123.0, 15))

    def test_rx_lsb_entry_and_mirrored_overlay(self):
        from pluto_advanced_rx import gui, flowgraph as rxf
        from tests import fakes
        w = gui.MainWindow("x")
        LSBM, SSBM = rxf.AdvancedRxFlowgraph.MODE_LSB, rxf.AdvancedRxFlowgraph.MODE_SSB
        self.assertNotEqual(w.demod_combo.findData(LSBM), -1)
        fakes.make_fake_rx(np.zeros(1_000_000, np.complex64), 1_000_000)
        w.tb = rxf.AdvancedRxFlowgraph(uri="x", frequency=14_200_000.0, sample_rate=1_000_000, device_type="fake")
        bands = []
        w.waterfall.set_demod_band = lambda lo, hi: bands.append((lo, hi))
        w.tb.start()
        self.addCleanup(lambda: (w.tb.stop(), w.tb.wait()))
        freq = 14_200_000.0
        f_lo, width = config_rx.SSB_AUDIO_BAND_HZ[0], w.tb.ssb_demod_width_hz
        w.demod_combo.setCurrentIndex(w.demod_combo.findData(SSBM))
        w._sync_waterfall()
        self.assertAlmostEqual(bands[-1][0], freq + f_lo)
        w.demod_combo.setCurrentIndex(w.demod_combo.findData(LSBM))
        self.assertEqual(w.tb.demod_mode, LSBM)
        w._sync_waterfall()
        self.assertAlmostEqual(bands[-1][0], freq - f_lo - width)
        self.assertAlmostEqual(bands[-1][1], freq - f_lo)


if __name__ == "__main__":
    unittest.main()
