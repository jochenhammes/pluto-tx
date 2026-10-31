"""APRS through the real RX flowgraph and CLI (GNU Radio blocks, fake device replaying synthetic IQ)."""
import contextlib
import io
import json
import shutil
import signal
import subprocess
import sys
import time
import unittest

import numpy as np

from pluto_advanced_rx import config
from pluto_tx import aprs, aprs_codec as c


def fm_iq(audio, audio_rate, rate, offset_hz=0.0, snr_db=None, seed=1):
    """Audio as FM at +-3 kHz deviation, the way a radio sends it."""
    t = np.arange(int(len(audio) * rate / audio_rate))
    a = np.interp(t * audio_rate / rate, np.arange(len(audio)), audio)
    freq = a / aprs.AMPLITUDE * 3000.0 + offset_hz
    x = np.exp(2j * np.pi * np.cumsum(freq) / rate)
    if snr_db is not None:
        rng = np.random.default_rng(seed)
        sigma = np.sqrt(10 ** (-snr_db / 10) * rate / 12_500 / 2)
        x = x + sigma * (rng.standard_normal(len(x)) + 1j * rng.standard_normal(len(x)))
    return x.astype(np.complex64)


def scene(rate, frames, **kw):
    audio_rate = 48_000
    pad = np.zeros(int(0.1 * audio_rate), np.float32)
    audio = np.concatenate([np.concatenate([pad, aprs.encode_frame(f, audio_rate), pad]) for f in frames])
    return fm_iq(audio, audio_rate, rate, **kw)


def run(iq, rate, frequency=144.8e6):
    from pluto_advanced_rx import flowgraph as rxf
    from tests import fakes
    fakes.make_fake_rx(iq, rate)
    got = []
    rx = rxf.AdvancedRxFlowgraph(uri="x", frequency=frequency, sample_rate=rate, device_type="fake", active_digimode="aprs", on_aprs_frame=got.append)
    rx.start()
    rx.wait()
    return got, rx


FRAME = c.build_frame("DA2JH-9", "APRS", ["WIDE1-1"], b"!5330.00N/00900.00E>TEST")


class FlowgraphTests(unittest.TestCase):
    def test_each_device_rate(self):
        for rate in (1_000_000, 2_048_000, 2_400_000, 2_500_000, 4_000_000):
            with self.subTest(rate=rate):
                got, _ = run(scene(rate, [FRAME, FRAME]), rate)
                self.assertGreaterEqual(len(got), 1)
                self.assertEqual(got[0]["info"], b"!5330.00N/00900.00E>TEST")

    def test_frequency_error_and_noise(self):
        rate = 2_500_000
        frames = [c.build_frame("DA2JH-9", "APRS", [], f"!packet {i}".encode()) for i in range(6)]
        got, _ = run(scene(rate, frames, offset_hz=1500.0, snr_db=15), rate)
        self.assertEqual(len(got), 6)

    def test_other_modes_do_not_wire_it(self):
        from pluto_advanced_rx import flowgraph as rxf
        from tests import fakes
        fakes.make_fake_rx(np.zeros(100, np.complex64), 2_500_000)
        self.assertIsNone(rxf.AdvancedRxFlowgraph(uri="x", frequency=433e6, sample_rate=2_500_000, device_type="fake").aprs_deframer)


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
    def test_packets_as_json_on_the_default_frequency_and_silent(self):
        from unittest import mock
        from pluto_advanced_rx import flowgraph as rxf
        from tests import fakes
        rate = 2_500_000
        fakes.make_fake_rx(scene(rate, [FRAME]), rate)
        with mock.patch.object(rxf.AdvancedRxFlowgraph, "set_rx_muted") as muted:
            code, events = run_cli(["rx", "fm", "--device", "fake", "--uri", "x", "--digimode", "aprs", "--duration", "1", "--json"])
        self.assertEqual(code, 0)
        self.assertEqual(next(e for e in events if e["event"] == "aprs_listen")["freq_hz"], config.APRS_DEFAULT_FREQ_HZ)
        packet = next(e for e in events if e["event"] == "aprs_packet")
        self.assertEqual((packet["source"], packet["kind"]), ("DA2JH-9", "position"))
        self.assertAlmostEqual(packet["lat"], 53.5, places=3)
        self.assertNotIn(mock.call(False), muted.call_args_list)

    def test_explicit_frequency_wins(self):
        from tests import fakes
        fakes.make_fake_rx(np.zeros(1000, np.complex64), 2_500_000)
        _code, events = run_cli(["rx", "fm", "--device", "fake", "--uri", "x", "--freq", "144390000", "--digimode", "aprs", "--duration", "0.2", "--json"])
        self.assertEqual(next(e for e in events if e["event"] == "aprs_listen")["freq_hz"], 144390000)


def tx_iq(**settings):
    """Everything the transmit flowgraph sends, on a fake device: nothing leaves this process."""
    from pluto_tx import flowgraph as txf
    from tests import fakes
    fakes.register_tx()
    txf.PlutoTxFlowgraph._aprs_last_sent = -1e9
    fg = txf.PlutoTxFlowgraph(device_type="fake", mode=txf.PlutoTxFlowgraph.MODE_APRS, frequency=144.8e6, aprs_callsign="DA2JH-9",
                              aprs_path=("WIDE1-1",), aprs_lat=53.5, aprs_lon=9.0, aprs_comment="TEST", **settings)
    fg.start()
    time.sleep(0.3)
    fg.key_ptt()
    time.sleep(fg.aprs_hold_s + 0.3)
    fg.unkey_ptt()
    fg.stop()
    fg.wait()
    iq = np.array(fg.device.sink.data(), dtype=np.complex64)
    on = np.flatnonzero(np.abs(iq) > 0.01)
    return iq[on[0]:on[-1]], fg


class TransmitTests(unittest.TestCase):
    RATE = 2_500_000

    def test_beacon_through_tx_and_rx_flowgraphs(self):
        iq, fg = tx_iq()
        self.assertTrue(np.all(np.abs(np.abs(iq) - np.abs(iq).max()) < 0.2 * np.abs(iq).max()), "FM must stay constant envelope")
        rate = self.RATE
        pad = np.zeros(int(0.1 * rate), np.complex64)
        got, _ = run(np.concatenate([pad, iq, pad]), rate)
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]["source"], "DA2JH-9")
        self.assertEqual(got[0]["destination"], "APZPTX")
        self.assertEqual(got[0]["path"], ["WIDE1-1"])
        info = c.summarize(got[0])
        self.assertAlmostEqual(info["lat"], 53.5, places=3)
        self.assertEqual(info["comment"], "TEST")
        self.assertFalse(fg.keyed)

    def test_deviation_is_three_khz(self):
        iq, _ = tx_iq()
        freq = np.angle(iq[1:] * np.conj(iq[:-1])) * self.RATE / (2 * np.pi)
        self.assertAlmostEqual(np.percentile(np.abs(freq), 99.9), 3000, delta=250)

    def test_message_kind(self):
        iq, _ = tx_iq(aprs_kind="message", aprs_to="DL1ABC", aprs_text="Hallo", aprs_message_id="7")
        pad = np.zeros(int(0.1 * self.RATE), np.complex64)
        got, _ = run(np.concatenate([pad, iq, pad]), self.RATE)
        info = c.summarize(got[0])
        self.assertEqual((info["kind"], info["addressee"], info["message"], info["message_id"]), ("message", "DL1ABC", "Hallo", "7"))

    def test_independent_decoder_reads_what_we_send(self):
        if shutil.which("multimon-ng") is None:
            self.skipTest("multimon-ng is not installed")
        iq, _ = tx_iq()
        freq = np.angle(iq[1:] * np.conj(iq[:-1])) * self.RATE / (2 * np.pi) / 3000.0
        n = int(len(freq) * 22050 / self.RATE)
        audio = np.convolve(freq, np.ones(50) / 50, "same")[(np.arange(n) * self.RATE / 22050).astype(int)]
        pcm = (np.clip(audio * 0.4, -1, 1) * 32767).astype("<i2").tobytes() + bytes(44100)
        out = subprocess.run(["multimon-ng", "-q", "-t", "raw", "-a", "AFSK1200", "-"], input=pcm, capture_output=True, timeout=60).stdout.decode("latin-1")
        self.assertIn("fm DA2JH-9 to APZPTX-0 via WIDE1-1", out)
        self.assertIn("5330.00N/00900.00E-TEST", out)


class RefusalTests(unittest.TestCase):
    def tb(self, frequency=144.8e6, **settings):
        from pluto_tx import flowgraph as txf
        from tests import fakes
        fakes.register_tx()
        txf.PlutoTxFlowgraph._aprs_last_sent = -1e9
        return txf.PlutoTxFlowgraph(device_type="fake", mode=txf.PlutoTxFlowgraph.MODE_APRS, frequency=frequency, **settings)

    def test_reasons(self):
        good = dict(aprs_callsign="DA2JH", aprs_lat=53.5, aprs_lon=9.0)
        self.assertIsNone(self.tb(**good).aprs_problem())
        self.assertIn("no callsign", self.tb(aprs_lat=53.5, aprs_lon=9.0).aprs_problem())
        self.assertIn("invalid callsign", self.tb(aprs_callsign="bad call", aprs_lat=53.5, aprs_lon=9.0).aprs_problem())
        self.assertIn("outside the amateur bands", self.tb(frequency=868.2e6, **good).aprs_problem())
        self.assertIn("needs a position", self.tb(aprs_callsign="DA2JH").aprs_problem())
        self.assertIn("too long", self.tb(aprs_callsign="DA2JH", aprs_kind="message", aprs_to="DL1ABC", aprs_text="x" * 68).aprs_problem())

    def test_nothing_starts_when_refused(self):
        fg = self.tb(frequency=868.2e6, aprs_callsign="DA2JH", aprs_lat=53.5, aprs_lon=9.0)
        with self.assertRaisesRegex(ValueError, "amateur bands"):
            fg.key_ptt()
        self.assertFalse(fg.keyed)

    def test_rate_limit_survives_a_rebuild(self):
        from pluto_tx import flowgraph as txf
        fg = self.tb(aprs_callsign="DA2JH", aprs_lat=53.5, aprs_lon=9.0)
        fg._ensure_aprs_audio()
        fg._aprs_start_source()
        again = txf.PlutoTxFlowgraph(device_type="fake", mode=txf.PlutoTxFlowgraph.MODE_APRS, frequency=144.8e6, aprs_callsign="DA2JH", aprs_lat=53.5, aprs_lon=9.0)
        self.assertIn("wait", again.aprs_problem())
        txf.PlutoTxFlowgraph._aprs_last_sent = -1e9

    def test_esp32_cannot_send_it(self):
        from pluto_tx.devices.espdr import EspdrDevice
        self.assertFalse(EspdrDevice.supports_mode("aprs"))


class CliTransmitTests(unittest.TestCase):
    def setUp(self):
        from pluto_tx import flowgraph as txf
        from tests import fakes
        fakes.register_tx()
        txf.PlutoTxFlowgraph._aprs_last_sent = -1e9

    def test_needs_an_explicit_frequency(self):
        code, events = run_cli(["tx", "aprs", "--device", "fake", "--uri", "x", "--call", "DA2JH", "--locator", "JO43", "--yes", "--json"])
        self.assertEqual(code, 1)
        self.assertIn("give --freq explicitly", json.dumps(events))

    def test_beacon_from_locator(self):
        code, events = run_cli(["tx", "aprs", "--device", "fake", "--uri", "x", "--freq", "144800000", "--call", "DA2JH", "--locator", "JO43",
                                "--comment", "TEST", "--path", "none", "--yes", "--json"])
        self.assertEqual(code, 0)
        packet = next(e for e in events if e["event"] == "aprs_packet")
        self.assertEqual((packet["direction"], packet["kind"], packet["path"]), ("tx", "position", []))
        self.assertAlmostEqual(packet["lat"], 53.5, places=3)
        self.assertIn("keyed", [e["event"] for e in events])

    def test_refused_before_anything_is_keyed(self):
        code, events = run_cli(["tx", "aprs", "--device", "fake", "--uri", "x", "--freq", "868200000", "--call", "DA2JH", "--locator", "JO43", "--yes", "--json"])
        self.assertEqual(code, 1)
        self.assertNotIn("keyed", [e["event"] for e in events])


if __name__ == "__main__":
    unittest.main()
