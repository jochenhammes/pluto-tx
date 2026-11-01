"""JTTY transmit through the real flowgraph (fake device): the signal it sends must decode with WSJT-X's own decoder."""
import io
import json
import time
import unittest
from contextlib import redirect_stdout
from unittest import mock

import numpy as np
from gnuradio import blocks

from pluto_tx import config, jtty_codec
from pluto_tx import flowgraph as txf

JTTY = txf.PlutoTxFlowgraph.MODE_JTTY
RATE = 2_500_000
FREQ = 432.15e6


def usb_audio(iq, rate=RATE):
    """What a USB receiver hears: the real part of the baseband, at 12 kHz."""
    from scipy.signal import resample_poly
    return resample_poly(iq.real, jtty_codec.SAMPLE_RATE, rate)


def to_pcm(audio):
    gap = np.zeros(jtty_codec.SAMPLE_RATE // 2)
    x = np.concatenate([gap, audio, gap])
    return (x / np.sqrt(np.mean(x ** 2)) * 3000.0).clip(-32767, 32767).astype("<i2").tobytes()


def send(text, tone_hz=1500.0, device_type="fake", frequency=FREQ):
    fg = txf.PlutoTxFlowgraph(device_type=device_type, mode=JTTY, frequency=frequency, jtty_text=text, jtty_tone_hz=tone_hz)
    fg.start()
    time.sleep(0.4)
    fg.key_ptt()
    time.sleep(fg.jtty_hold_s + 0.2)
    fg.unkey_ptt()
    fg.stop()
    fg.wait()
    iq = np.array(fg.device.sink.data(), dtype=np.complex64)
    return iq, fg


@unittest.skipUnless(jtty_codec.available(), "wsjt-codec not installed (install-jtty.sh)")
class TransmitTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from tests import fakes
        fakes.register_tx()

    def test_sent_signal_decodes_with_the_original_decoder(self):
        text = "CQ DA2JH CQ TEST 73"
        iq, fg = send(text)
        on = np.flatnonzero(np.abs(iq) > 0.01)
        burst = iq[on[0]:on[-1]]
        self.assertAlmostEqual(len(burst) / RATE, fg.jtty_rendered.duration_s, delta=0.05)
        finals = jtty_codec.decode_pcm(to_pcm(usb_audio(iq[on[0]:on[-1]])))
        self.assertEqual([(m["text"], m["state"]) for m in finals], [(text, "complete")])
        self.assertAlmostEqual(finals[0]["freq"], 1500.0, delta=2.0)

    def test_signal_is_one_sided_and_constant_envelope(self):
        iq, _fg = send("CQ DA2JH CQ", tone_hz=1000.0)
        on = np.flatnonzero(np.abs(iq) > 0.01)
        burst = iq[on[0] + RATE // 10:on[-1] - RATE // 10]
        self.assertAlmostEqual(float(np.percentile(np.abs(burst), 50)), config.JTTY_AUDIO_LEVEL, delta=0.05)
        power = np.abs(np.fft.fft(burst)) ** 2
        f = np.fft.fftfreq(len(burst), 1 / RATE)
        self.assertGreater(power[(f > 900) & (f < 1250)].sum() / power.sum(), 0.97)
        self.assertLess(power[f < 0].sum() / power.sum(), 0.001)  # USB: nothing below the carrier

    def test_ptt_off_ends_the_transmission(self):
        fg = txf.PlutoTxFlowgraph(device_type="fake", mode=JTTY, frequency=FREQ, jtty_text="X" * 80)
        fg.start()
        time.sleep(0.4)
        fg.key_ptt()
        time.sleep(0.5)
        fg.unkey_ptt()
        self.assertFalse(fg._keyed)
        time.sleep(0.3)
        before = len(fg.device.sink.data())
        time.sleep(0.3)
        tail = np.array(fg.device.sink.data()[before:], dtype=np.complex64)
        fg.stop()
        fg.wait()
        self.assertLess(float(np.abs(tail).max(initial=0.0)), 0.001)

    def test_refused_before_any_rf_action(self):
        cases = [("X" * 81, FREQ, "longer"), ("", FREQ, "empty"), ("a\nb", FREQ, "one line"), ("TEST", 869.6e6, "amateur bands")]
        for text, freq, why in cases:
            fg = txf.PlutoTxFlowgraph(device_type="fake", mode=JTTY, frequency=freq, jtty_text=text)
            self.assertRegex(fg.jtty_problem(), why)
            with mock.patch.object(fg.device, "pre_key") as pre_key:
                with self.assertRaisesRegex(ValueError, why):
                    fg.key_ptt()
                pre_key.assert_not_called()
            self.assertFalse(fg._keyed)

    def test_problem_without_the_codec(self):
        fg = txf.PlutoTxFlowgraph(device_type="fake", mode=JTTY, frequency=FREQ, jtty_text="TEST")
        with mock.patch.object(jtty_codec, "codec_program", return_value=None):
            self.assertRegex(fg.jtty_problem(), "install-jtty.sh")

    def test_settings_change_is_rendered_again(self):
        fg = txf.PlutoTxFlowgraph(device_type="fake", mode=JTTY, frequency=FREQ, jtty_text="ONE")
        fg._ensure_jtty_audio()
        first = fg.jtty_rendered
        fg.set_jtty(text="TWO")
        fg._ensure_jtty_audio()
        self.assertEqual(fg.jtty_rendered.text, "TWO")
        self.assertIsNot(first, fg.jtty_rendered)

    def test_mode_switch_keeps_other_modes_quiet(self):
        fg = txf.PlutoTxFlowgraph(device_type="fake", mode=txf.PlutoTxFlowgraph.MODE_FM, frequency=FREQ)
        fg.set_mode(JTTY)
        self.assertEqual(fg.mode, JTTY)
        fg.set_mode(txf.PlutoTxFlowgraph.MODE_RTTY)
        self.assertEqual(fg.mode, txf.PlutoTxFlowgraph.MODE_RTTY)


@unittest.skipUnless(jtty_codec.available(), "wsjt-codec not installed (install-jtty.sh)")
class DriftTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from tests import fakes
        fakes.register_tx()

    def make(self, text="X" * 40):
        return txf.PlutoTxFlowgraph(device_type="fake", mode=JTTY, frequency=1_296_200_000.0, jtty_text=text)

    def test_chirp_runs_against_the_drift(self):
        rate = config.AUDIO_RATE
        t = np.arange(rate * 10) / rate
        out = txf._opposite_chirp(np.cos(2 * np.pi * 1000.0 * t).astype(np.float32), -4.0, rate)
        def peak(x):
            spec = np.abs(np.fft.rfft(x * np.hanning(len(x)), 8 * len(x)))
            return np.argmax(spec) * rate / (8.0 * len(x))
        self.assertAlmostEqual(peak(out[:rate]), 1000.0 + 2.0, delta=0.5)
        self.assertAlmostEqual(peak(out[-rate:]), 1000.0 + 38.0, delta=0.5)
        self.assertAlmostEqual(float(np.max(np.abs(out[rate:-rate]))), 1.0, delta=0.02)

    def test_compensated_signal_decodes_after_the_modelled_drift(self):
        """A carrier that drifts as predicted: what arrives must stay on its frequency instead of sliding."""
        fg = self.make()
        with mock.patch.object(fg, "jtty_drift_hz_per_s", return_value=-4.0):
            fg._ensure_jtty_audio()
        audio = fg._jtty_audio
        rate = config.AUDIO_RATE
        t = np.arange(len(audio)) / rate
        from scipy.signal import hilbert, resample_poly
        on_air = (hilbert(audio) * np.exp(2j * np.pi * (0.5 * -4.0 * t * t))).real
        finals = jtty_codec.decode_pcm(to_pcm(resample_poly(on_air, jtty_codec.SAMPLE_RATE, rate)))
        self.assertEqual([m["text"] for m in finals], [fg.jtty_rendered.text])
        self.assertAlmostEqual(finals[0]["freq"], 1500.0, delta=2.0)

    def test_devices_without_a_model_are_not_chirped(self):
        fg = self.make()
        fg._ensure_jtty_audio()
        self.assertEqual(fg.jtty_drift_comp_hz_s, 0.0)
        np.testing.assert_array_almost_equal(fg._jtty_audio[:1000], fg.jtty_rendered.audio[:1000] * config.JTTY_AUDIO_LEVEL)

    def test_switch_turns_it_off(self):
        fg = self.make()
        fg.ft8_drift_comp_enabled = False
        with mock.patch.dict(config.FT8_DRIFT_MODEL, {"fake": config.FT8_DRIFT_MODEL["pluto"]}):
            self.assertEqual(fg.jtty_drift_hz_per_s(), 0.0)

    def test_a_long_pause_counts_as_cold(self):
        model = config.FT8_DRIFT_MODEL["pluto"]
        with mock.patch.dict(config.FT8_DRIFT_MODEL, {"fake": model}):
            fg = self.make()
            hz = 1_296_200_000.0 * 1e-9
            cold = (model["tx_ppb_s"] + model["warmup_ppb_s"]) * hz
            fg.restore_drift_history(3600.0, 60.0)  # warm
            warm = fg.jtty_drift_hz_per_s()
            self.assertLess(abs(warm), abs(cold) * 0.6)
            fg.restore_drift_history(3600.0, config.JTTY_DRIFT_COLD_PAUSE_S + 1)
            self.assertAlmostEqual(fg.jtty_drift_hz_per_s(), cold, delta=0.05)

    def test_unkey_notes_the_end_of_the_transmission(self):
        fg = self.make("TEST")
        fg.start()
        time.sleep(0.3)
        fg.key_ptt()
        time.sleep(0.2)
        fg.unkey_ptt()
        self.assertIsNotNone(fg._ft8_last_tx_end)
        fg.stop()
        fg.wait()


@unittest.skipUnless(jtty_codec.available(), "wsjt-codec not installed (install-jtty.sh)")
class SoundcardTests(unittest.TestCase):
    """Soundcard and AIOC take the audio itself: the radio's USB makes the signal."""

    @classmethod
    def setUpClass(cls):
        from tests import fakes
        fakes.register_fake_audio_tx()

    def test_audio_reaches_the_output_and_decodes(self):
        text = "CQ DA2JH CQ SOUNDCARD"
        fg = txf.PlutoTxFlowgraph(device_type="fake_audio", mode=JTTY, frequency=FREQ, jtty_text=text)
        capture = blocks.vector_sink_f()
        fg.connect(fg.jtty_audio_gain, capture)
        fg.start()
        time.sleep(0.3)
        fg.key_ptt()
        self.assertTrue(fg._keyed)
        time.sleep(fg.jtty_hold_s + 0.2)
        fg.unkey_ptt()
        self.assertFalse(fg._keyed)
        fg.stop()
        fg.wait()
        from scipy.signal import resample_poly
        audio = np.array(capture.data(), dtype=np.float32)
        on = np.flatnonzero(np.abs(audio) > 0.01)
        burst = audio[on[0]:on[-1]]
        self.assertAlmostEqual(float(np.abs(burst).max()), config.JTTY_SOUNDCARD_LEVEL, delta=0.02)
        finals = jtty_codec.decode_pcm(to_pcm(resample_poly(burst, jtty_codec.SAMPLE_RATE, config.AUDIO_RATE)))
        self.assertEqual([(m["text"], m["state"]) for m in finals], [(text, "complete")])

    def test_works_without_an_amateur_band_frequency(self):
        fg = txf.PlutoTxFlowgraph(device_type="fake_audio", mode=JTTY, frequency=869.6e6, jtty_text="TEST")
        self.assertIsNone(fg.jtty_problem())  # the radio's own frequency is not known here


@unittest.skipUnless(jtty_codec.available(), "wsjt-codec not installed (install-jtty.sh)")
class CliTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from tests import fakes
        fakes.register_tx()

    def run_cli(self, *argv):
        from pluto_cli import app
        out = io.StringIO()
        with redirect_stdout(out):
            try:
                code = app.main(["tx", "jtty", "--device", "fake", "--uri", "x", "--json", "--yes", *argv])
            except SystemExit as e:
                code = e.code
        return code, [json.loads(line) for line in out.getvalue().splitlines() if line.startswith("{")]

    def test_sends_one_message(self):
        code, events = self.run_cli("--freq", str(int(FREQ)), "--text", "CQ DA2JH CQ")
        self.assertEqual(code, 0)
        msg = next(e for e in events if e["event"] == "jtty_message")
        self.assertEqual(msg["text"], "CQ DA2JH CQ")
        self.assertEqual(msg["frames"], ["CQ DA2JH CQ"])

    def test_needs_an_explicit_frequency(self):
        code, events = self.run_cli("--text", "TEST")
        self.assertEqual(code, 1)
        self.assertIn("--freq", json.dumps(events))

    def test_refuses_outside_the_amateur_bands(self):
        code, events = self.run_cli("--freq", "869600000", "--text", "TEST")
        self.assertEqual(code, 1)
        self.assertIn("amateur bands", json.dumps(events))


if __name__ == "__main__":
    unittest.main()
