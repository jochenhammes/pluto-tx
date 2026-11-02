"""JTTY receive: the receiver block, its state, and the whole chain TX flowgraph -> IQ -> RX flowgraph -> wsjt-codec."""
import json
import os
import threading
import time
import unittest
import wave

import numpy as np

from pluto_tx import jtty_codec

RATE = 2_500_000
SAMPLE = os.path.join(os.path.dirname(__file__), "data", "jtty", "260807_134110.wav")
skip_without_codec = unittest.skipUnless(jtty_codec.available(), "wsjt-codec not installed (./install.sh jtty)")


def feed(block, audio, chunk=4096):
    for i in range(0, len(audio), chunk):
        block.work([audio[i:i + chunk]], [])


def wait_until(condition, timeout=10.0):
    end = time.time() + timeout
    while time.time() < end:
        if condition():
            return True
        time.sleep(0.02)
    return condition()


@skip_without_codec
class ReceiverTests(unittest.TestCase):
    def make(self, **kw):
        from pluto_advanced_rx.jtty_rx import JttyReceiver
        self.updates = []
        rx = JttyReceiver(self.updates.append, **kw)
        rx.start()
        self.addCleanup(rx.stop)
        return rx

    def sample(self):
        with wave.open(SAMPLE) as w:
            return np.frombuffer(w.readframes(w.getnframes()), dtype="<i2").astype(np.float32) / 32768.0

    def assertSampleDecoded(self):
        self.assertIn(("RAN ALL NIGHT ON BAND NOISE - NO FALSE DECODES!", "complete"), [(u["text"], u["state"]) for u in self.updates])

    def test_sample_recording_grows_message_by_message(self):
        rx = self.make(clock=lambda: 1000.0)
        feed(rx, self.sample())
        rx.stop()
        self.assertSampleDecoded()
        growing = [u["text"] for u in self.updates if u["id"] == self.updates[0]["id"] and u["state"] == "growing"]
        self.assertGreater(len(growing), 3)
        self.assertTrue(all(b.startswith(a) for a, b in zip(growing, growing[1:])))

    def test_level_does_not_matter(self):
        for scale in (1e-4, 1.0, 50.0):
            with self.subTest(scale=scale):
                rx = self.make()
                feed(rx, self.sample() * scale)
                rx.stop()
                self.assertSampleDecoded()

    def test_time_comes_from_the_position_in_the_stream(self):
        now = [1_800_000_000.0]
        rx = self.make(clock=lambda: now[0])
        audio = self.sample()
        for i in range(0, len(audio), 4096):
            now[0] += 4096 / jtty_codec.SAMPLE_RATE
            rx.work([audio[i:i + 4096]], [])
            time.sleep(0.0005)
        rx.stop()
        first = self.updates[0]
        expected = 1_800_000_000.0 + first["start"]
        self.assertAlmostEqual(first["utc_start"], expected, delta=2.0)

    def test_full_queue_drops_and_counts_without_blocking(self):
        from pluto_advanced_rx import jtty_rx
        release = threading.Event()

        class Stalled:
            def __init__(self, on_event, on_exit):
                pass

            def configure(self, *a):
                pass

            def control(self, r):
                pass

            def send_audio(self, pcm):
                release.wait(10)

            def close(self):
                pass
        rx = jtty_rx.JttyReceiver(lambda u: None, stream_factory=Stalled)
        rx.start()
        t0 = time.time()
        block = np.zeros(jtty_rx.SAMPLE_RATE, np.float32)
        for _ in range(jtty_rx.config.JTTY_QUEUE_S + 5):
            self.assertEqual(rx.work([block], []), len(block))
        self.assertLess(time.time() - t0, 2.0)
        self.assertGreater(rx.dropped_samples, 0)
        release.set()
        rx.stop()

    def test_crash_restarts_once_then_reports(self):
        from pluto_advanced_rx import jtty_rx
        made = []

        class Crashing:
            def __init__(self, on_event, on_exit):
                self.on_exit = on_exit
                made.append(self)

            def configure(self, *a):
                pass

            def control(self, r):
                pass

            def send_audio(self, pcm):
                pass

            def close(self):
                pass
        rx = jtty_rx.JttyReceiver(lambda u: None, stream_factory=Crashing)
        rx.start()
        made[-1].on_exit(1, False)
        self.assertEqual(len(made), 2)
        self.assertIn("restarted", rx.last_error)
        made[-1].on_exit(1, False)
        self.assertEqual(len(made), 2)
        self.assertIn("ended", rx.last_error)
        rx.stop()

    def test_missing_codec_is_reported_not_raised(self):
        from pluto_advanced_rx import jtty_rx

        def missing(on_event, on_exit):
            raise RuntimeError("wsjt-codec is not installed")
        rx = jtty_rx.JttyReceiver(lambda u: None, stream_factory=missing)
        self.assertTrue(rx.start())
        rx.work([np.zeros(100, np.float32)], [])
        rx.stop()
        self.assertIn("not installed", rx.last_error)


class StateTests(unittest.TestCase):
    def make(self):
        from pluto_advanced_rx.jtty_state import JttyState
        return JttyState(clock=lambda: 1000.0)

    def test_one_row_per_message(self):
        st = self.make()
        st.on_update(dict(id=1, text="", state="growing", freq=1500.0, snr=-3, utc_start=900.0))
        self.assertEqual(st.get_rows(), [])
        st.on_update(dict(id=1, text="HEL", state="growing", freq=1500.0, snr=-3, utc_start=900.0))
        st.on_update(dict(id=1, text="HELLO", state="complete", freq=1501.0, snr=-2, utc_start=900.5, gaps=[3]))
        rows = st.get_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["text"], rows[0]["state"], rows[0]["gaps"], rows[0]["snr"]), ("HELLO", "complete", [3], -3))
        self.assertEqual(rows[0]["time"], 900.0)

    def test_empty_last_line_keeps_the_text(self):
        st = self.make()
        st.on_update(dict(id=1, text="HEL", state="growing", utc_start=1.0))
        st.on_update(dict(id=1, text="", state="ended", utc_start=1.0))
        self.assertEqual((st.get_rows()[0]["text"], st.get_rows()[0]["state"]), ("HEL", "ended"))

    def test_take_changed_and_generations(self):
        st = self.make()
        st.on_update(dict(id=1, text="A", state="complete"))
        self.assertEqual(len(st.take_changed()), 1)
        self.assertEqual(st.take_changed(), [])
        st.new_generation()
        st.on_update(dict(id=1, text="B", state="complete"))  # a restarted codec counts from 1 again
        self.assertEqual([r["text"] for r in st.get_rows()], ["A", "B"])

    def test_sent_is_marked_and_limit_holds(self):
        from pluto_advanced_rx import jtty_state
        st = self.make()
        st.add_sent("CQ", 1500.0)
        self.assertEqual(st.get_rows()[0]["direction"], "tx")
        for i in range(jtty_state.MAX_MESSAGES + 5):
            st.on_update(dict(id=i, text="X", state="complete"))
        self.assertEqual(len(st.get_rows()), jtty_state.MAX_MESSAGES)
        st.clear()
        self.assertEqual(st.get_rows(), [])


@skip_without_codec
class WholeChainTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from tests import fakes
        from pluto_tx import flowgraph as txf
        fakes.register_tx()
        fg = txf.PlutoTxFlowgraph(device_type="fake", mode=txf.PlutoTxFlowgraph.MODE_JTTY, frequency=432.15e6, jtty_text="CQ DA2JH CQ HALLO 73")
        fg.start()
        time.sleep(0.4)
        fg.key_ptt()
        time.sleep(fg.jtty_hold_s + 0.2)
        fg.unkey_ptt()
        fg.stop()
        fg.wait()
        iq = np.array(fg.device.sink.data(), dtype=np.complex64)
        on = np.flatnonzero(np.abs(iq) > 0.01)
        cls.iq = iq[on[0]:on[-1]]

    def receive(self, offset_hz=0.0, snr_db=None):
        from pluto_advanced_rx import flowgraph as rxf
        from tests import fakes
        n = np.arange(len(self.iq))
        x = self.iq * np.exp(2j * np.pi * offset_hz * n / RATE).astype(np.complex64)
        pad = np.zeros(RATE, np.complex64)
        scene = np.concatenate([pad, x, pad])
        if snr_db is not None:  # SNR in 2500 Hz, the WSJT definition
            sigma = np.sqrt(np.mean(np.abs(x) ** 2) / 10 ** (snr_db / 10) * (RATE / 2500) / 2)
            scene = scene + (sigma * (np.random.randn(len(scene)) + 1j * np.random.randn(len(scene)))).astype(np.complex64)
        fakes.make_fake_rx(scene.astype(np.complex64), RATE)
        got = []
        rx = rxf.AdvancedRxFlowgraph(uri="x", frequency=432.15e6, sample_rate=RATE, device_type="fake", demod_mode=rxf.AdvancedRxFlowgraph.MODE_SSB,
                                     active_digimode="jtty", on_jtty_update=got.append)
        rx.start()
        time.sleep(len(scene) / RATE + 1.5)
        rx.stop()
        rx.wait()
        return got

    def test_high_band_widens_the_search(self):
        from pluto_advanced_rx import config as rxc, flowgraph as rxf
        from tests import fakes
        fakes.make_fake_rx(np.zeros(1000, np.complex64), RATE)
        rx = rxf.AdvancedRxFlowgraph(uri="x", frequency=432.15e6, sample_rate=RATE, device_type="fake", active_digimode="jtty")
        self.assertEqual(rx.jtty_receiver._search[1], rxc.JTTY_RX_TOLERANCE_HZ)
        rx.set_frequency(1_296_200_000.0)
        self.assertEqual(rx.jtty_receiver._search[1], rxc.JTTY_HIGH_BAND_TOLERANCE_HZ)
        rx.set_jtty_search(tolerance_hz=20)
        self.assertEqual(rx.jtty_receiver._search[1], rxc.JTTY_HIGH_BAND_TOLERANCE_HZ)
        rx.set_frequency(432.15e6)
        self.assertEqual(rx.jtty_receiver._search[1], 20)

    def assertDecoded(self, got):
        self.assertIn("CQ DA2JH CQ HALLO 73", [u["text"] for u in got if u["state"] == "complete"])

    def test_clean(self):
        self.assertDecoded(self.receive())

    def test_frequency_offset_within_the_tolerance(self):
        self.assertDecoded(self.receive(offset_hz=30.0))

    def test_weak_signal(self):
        self.assertDecoded(self.receive(snr_db=-10.0))


@skip_without_codec
class CliTests(unittest.TestCase):
    def test_emits_messages(self):
        import io
        from contextlib import redirect_stdout
        from pluto_advanced_rx.jtty_rx import SAMPLE_RATE
        from pluto_cli import app
        from scipy.signal import resample_poly
        from tests import fakes
        r = jtty_codec.render("CQ DA2JH CQ", rate=SAMPLE_RATE)
        analytic = np.exp(1j * np.unwrap(np.angle(__import__("scipy.signal").signal.hilbert(r.audio))))
        gap = np.zeros(RATE, np.complex64)
        iq = resample_poly(analytic, RATE // 500, SAMPLE_RATE // 500).astype(np.complex64)
        fakes.make_fake_rx(np.concatenate([gap, iq, gap]), RATE)
        out = io.StringIO()
        with redirect_stdout(out):
            try:
                code = app.main(["rx", "ssb", "--device", "fake", "--uri", "x", "--freq", "14090000", "--digimode", "jtty", "--duration", "4.5", "--json"])
            except SystemExit as e:
                code = e.code
        events = [json.loads(line) for line in out.getvalue().splitlines() if line.startswith("{")]
        self.assertIn(code, (0, None), out.getvalue())
        self.assertIn("CQ DA2JH CQ", [e.get("text") for e in events if e.get("event") == "jtty_message"])

    def test_audio_device_refuses_an_rf_frequency(self):
        import io
        from contextlib import redirect_stdout
        from pluto_cli import app
        out = io.StringIO()
        with redirect_stdout(out):
            code = app.main(["rx", "ssb", "--device", "audio", "--uri", "", "--freq", "14090000", "--digimode", "jtty", "--duration", "1", "--json"])
        self.assertEqual(code, 1)
        self.assertIn("no RF frequency", out.getvalue())


if __name__ == "__main__":
    unittest.main()
