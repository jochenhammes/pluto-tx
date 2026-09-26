"""FM voice audio quality: pre-/de-emphasis filters, TX pre-emphasis + selectable deviation on the
captured IQ of the fake device, the audio-only (Soundcard/AIOC) FM path staying un-emphasised, and
the RX app's de-emphasis."""
import os
import tempfile
import time
import unittest
from unittest import mock

import numpy as np
from gnuradio import blocks
from scipy import signal

from pluto_advanced_rx import config as config_rx
from pluto_tx import config, emphasis
from pluto_tx import flowgraph as txf
from tests.test_tx_subtone_lsb import RATE, fm_demod, line_amplitude, write_wav

FM = txf.PlutoTxFlowgraph.MODE_FM
LO_HZ, HI_HZ = 500.0, 2500.0
WAV_TEST = os.path.join(os.path.dirname(txf.__file__), "da2jh-test.wav")


def ratio_db(x, fs):
    return 20 * np.log10(line_amplitude(x, fs, HI_HZ) / line_amplitude(x, fs, LO_HZ))


class EmphasisPureTests(unittest.TestCase):
    def test_preemphasis_rises_6db_per_octave_and_is_0db_at_1khz(self):
        b, a = emphasis.preemphasis_taps(48_000, 750e-6, 12_000)
        self.assertAlmostEqual(emphasis.response_db(b, a, 48_000, 1000), 0.0, places=6)
        self.assertAlmostEqual(emphasis.response_db(b, a, 48_000, 3000) -
                               emphasis.response_db(b, a, 48_000, 300), 18.1, delta=1.0)
        self.assertAlmostEqual(emphasis.response_db(b, a, 48_000, 3000) -
                               emphasis.response_db(b, a, 48_000, 1500), 5.6, delta=0.8)

    def test_deemphasis_undoes_preemphasis_in_the_voice_band(self):
        pb, pa = emphasis.preemphasis_taps(48_000, 750e-6, 12_000)
        db_, da = emphasis.deemphasis_taps(48_000, 750e-6)
        for f in (300, 700, 1000, 2000, 3000):
            total = emphasis.response_db(pb, pa, 48_000, f) + emphasis.response_db(db_, da, 48_000, f)
            self.assertLess(abs(total), 1.0, f)


class TxTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from tests import fakes
        fakes.register_tx()
        fakes.register_fake_audio_tx()
        cls.tmp = tempfile.mkdtemp()
        t = np.arange(config.AUDIO_RATE * 4) / config.AUDIO_RATE
        cls.two_tone = os.path.join(cls.tmp, "two_tone.wav")
        write_wav(cls.two_tone, 0.3 * np.sin(2 * np.pi * LO_HZ * t) + 0.3 * np.sin(2 * np.pi * HI_HZ * t))

    def capture(self, wav, setup=None, seconds=2.2):
        fg = txf.PlutoTxFlowgraph(device_type="fake", mode=FM, source=txf.PlutoTxFlowgraph.SRC_FILE,
                                  wav_path=wav)
        if setup:
            setup(fg)
        fg.start()
        time.sleep(0.5)
        fg.key_ptt()
        time.sleep(seconds)
        fg.unkey_ptt()
        fg.stop()
        fg.wait()
        iq = np.array(fg.device.sink.data(), dtype=np.complex64)
        on = np.flatnonzero(np.abs(iq) > 0.01)
        return fm_demod(iq[on[0] + int(0.5 * RATE):on[0] + int((seconds - 0.2) * RATE)])

    def test_preemphasis_on_by_default_and_flat_after_receiver_deemphasis(self):
        # small NF gain keeps both tones below the clipper, so only the filters shape the ratio
        dem = self.capture(self.two_tone, setup=lambda fg: fg.set_nf_gain(0.1))
        expected = (emphasis.response_db(*emphasis.preemphasis_taps(48_000, 750e-6, 12_000), 48_000, HI_HZ) -
                    emphasis.response_db(*emphasis.preemphasis_taps(48_000, 750e-6, 12_000), 48_000, LO_HZ))
        self.assertAlmostEqual(ratio_db(dem, 50_000), expected, delta=1.5)
        b, a = emphasis.deemphasis_taps(50_000, 750e-6)
        self.assertLess(abs(ratio_db(signal.lfilter(b, a, dem), 50_000)), 1.5)

    def test_preemphasis_off_is_flat(self):
        dem = self.capture(self.two_tone, setup=lambda fg: (fg.set_nf_gain(0.1), fg.set_fm_preemphasis(False)))
        self.assertLess(abs(ratio_db(dem, 50_000)), 1.5)

    def test_speech_uses_the_selected_deviation_without_overshooting_it(self):
        for dev in config.FM_DEVIATION_CHOICES_HZ:
            with self.subTest(deviation=dev):
                dem = self.capture(WAV_TEST, setup=lambda fg: fg.set_fm_deviation(dev), seconds=4.0)
                dem = dem - np.mean(dem)
                self.assertLess(float(np.abs(dem).max()), 1.1 * dev)
                self.assertGreater(float(np.percentile(np.abs(dem), 99)), 0.5 * dev)

    def test_audio_only_fm_path_has_no_preemphasis_or_drive(self):
        sink = blocks.vector_sink_f()
        with mock.patch("pluto_tx.flowgraph.audio_devices.open_output_device", return_value=sink):
            fg = txf.PlutoTxFlowgraph(device_type="fake_audio", mode=FM, source=txf.PlutoTxFlowgraph.SRC_FILE,
                                      wav_path=self.two_tone)
            fg.start()
            time.sleep(0.3)
            sink.reset()
            fg.key_ptt()
            time.sleep(1.5)
            fg.unkey_ptt()
            fg.stop()
            fg.wait()
        audio = np.array(sink.data(), dtype=np.float32)
        audio = audio[len(audio) // 3:]
        self.assertGreater(len(audio), config.AUDIO_RATE // 2)
        self.assertLess(abs(ratio_db(audio, config.AUDIO_RATE)), 1.5)
        self.assertLess(float(np.abs(audio).max()), 0.6)       # the RF-only 12 dB drive would clip at 0.75

    def test_gui_fm_voice_row(self):
        from PyQt5 import QtWidgets
        from pluto_tx import gui
        TxTests.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
        w = gui.MainWindow("x")
        self.assertTrue(w.fm_voice_row_widget.isVisibleTo(w))                  # FM + Pluto (default)
        self.assertEqual(w.fm_deviation_combo.currentData(), config.FM_DEVIATION_HZ)
        self.assertTrue(w.fm_preemph_check.isChecked())
        w.fm_deviation_combo.setCurrentIndex(w.fm_deviation_combo.findData(5000.0))
        w.fm_preemph_check.setChecked(False)
        tb = txf.PlutoTxFlowgraph(device_type="fake", mode=FM, source=txf.PlutoTxFlowgraph.SRC_FILE)
        w._apply_fm_voice(tb)                                                   # what _rebuild() does
        self.assertEqual((tb.fm_deviation_hz, tb.fm_preemphasis), (5000.0, False))
        w.mode_combo.setCurrentIndex(w.mode_combo.findData(txf.PlutoTxFlowgraph.MODE_SSB))
        self.assertFalse(w.fm_voice_row_widget.isVisibleTo(w))
        w.mode_combo.setCurrentIndex(w.mode_combo.findData(FM))
        w.device_type_combo.setCurrentIndex(w.device_type_combo.findData("soundcard"))
        self.assertFalse(w.fm_voice_row_widget.isVisibleTo(w))                 # the radio does it itself


class RxDeemphasisTests(unittest.TestCase):
    def rx_audio(self, deemphasis, rate=1_000_000):
        from pluto_advanced_rx import flowgraph as rxf
        from tests import fakes
        t = np.arange(rate * 3) / rate
        m = np.sin(2 * np.pi * LO_HZ * t) + np.sin(2 * np.pi * HI_HZ * t)
        iq = (0.5 * np.exp(1j * 2 * np.pi * 1000 * np.cumsum(m) / rate)).astype(np.complex64)
        fakes.make_fake_rx(iq, rate)
        rx = rxf.AdvancedRxFlowgraph(uri="x", frequency=145_500_000.0, sample_rate=rate, device_type="fake",
                                     fm_deemphasis=deemphasis)
        sink = blocks.vector_sink_f()
        rx.connect(rx.demod_selector, sink)
        rx.start()
        time.sleep(2.5)
        rx.stop()
        rx.wait()
        audio = np.array(sink.data(), dtype=np.float32)
        return audio[len(audio) // 2:]

    def test_deemphasis_on_by_default_and_switchable(self):
        self.assertTrue(config_rx.FM_DEEMPH_DEFAULT)
        b, a = emphasis.deemphasis_taps(48_000, 750e-6)
        expected = emphasis.response_db(b, a, 48_000, HI_HZ) - emphasis.response_db(b, a, 48_000, LO_HZ)
        self.assertAlmostEqual(ratio_db(self.rx_audio(True), config_rx.AUDIO_RATE), expected, delta=1.5)
        self.assertLess(abs(ratio_db(self.rx_audio(False), config_rx.AUDIO_RATE)), 1.5)


if __name__ == "__main__":
    unittest.main()
