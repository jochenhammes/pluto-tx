"""APRS through the real RX flowgraph and CLI (GNU Radio blocks, fake device replaying synthetic IQ)."""
import contextlib
import io
import json
import signal
import sys
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


if __name__ == "__main__":
    unittest.main()
