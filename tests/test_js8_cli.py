"""J4: `pluto-cli tx js8` and `pluto-cli rx ... --digimode js8` with fake devices."""
import contextlib
import io
import json
import os
import signal
import sys
import threading
import time
import unittest
from unittest import mock

import numpy as np

from pluto_tx import js8, js8_message as M, js8_phy as P


def run_cli(argv):
    """-> (exit code, [event dicts]) of pluto_cli.app.main(argv) with --json output."""
    from pluto_cli import app
    out = io.StringIO()
    saved = (signal.getsignal(signal.SIGINT), signal.getsignal(signal.SIGTERM), sys.excepthook)
    try:
        with contextlib.redirect_stdout(out):
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


TX = ["tx", "js8", "--device", "fake", "--freq", "144178000", "--mycall", "DA2JH", "--mygrid", "JO31",
      "--submode", "turbo", "--yes", "--json"]


class TxCliTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from tests import fakes
        fakes.register_tx()

    def test_argument_checks(self):
        from pluto_cli import app
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            app.build_parser().parse_args(["tx", "js8", "--submode", "ludicrous"])
        for extra, msg in ((["--mycall", ""], "--mycall"), (["--kind", "ack"], "no message"),
                           (["--interactive"], "interactive"),
                           (["--kind", "allcall", "--text", "X" * 400], "too long|cannot"),
                           (["--freq", "100000000"], "amateur band")):
            code, events = run_cli(TX + extra)
            self.assertEqual(code, 1, extra)
            errors = [e["message"] for e in events if e["event"] == "error"]
            self.assertTrue(errors and any(__import__("re").search(msg, m) for m in errors), (extra, events))
            self.assertNotIn("keyed", [e["event"] for e in events])

    def test_tx_sequence_events(self):
        code, events = run_cli(TX + ["--kind", "allcall", "--text", "JS8 SEQUENCE TEST DE DA2JH"])
        self.assertEqual(code, 0)
        names = [e["event"] for e in events]
        n = M.build_frames("DA2JH", "JO31", "@ALLCALL JS8 SEQUENCE TEST DE DA2JH", P.TURBO)
        self.assertEqual(len(n), 3)
        self.assertEqual(names[0], "js8_message")
        self.assertEqual(events[0]["frames"], 3)
        self.assertEqual(events[0]["text"], "DA2JH: @ALLCALL  JS8 SEQUENCE TEST DE DA2JH")
        self.assertIn("js8_waiting", names)
        self.assertEqual(names.count("keyed"), 3)
        self.assertEqual(names.count("unkeyed"), 3)
        self.assertEqual([e["frame_no"] for e in events if e["event"] == "js8_frame_sent"], [1, 2, 3])
        self.assertEqual(names[-2:], ["js8_done", "shutdown"])

    def test_sigint_mid_sequence_is_safe(self):
        from tests import fakes
        safe_calls = []
        orig = fakes.FakeTxDevice.force_safe_state

        def spy(self):
            safe_calls.append(time.time())
            orig(self)

        def interrupt_during_first_frame():
            # the first frame starts at the next 6 s boundary + 0.1 s; interrupt 1 s into it
            now = time.time()
            start = js8.plan_frames(now, P.TURBO, 1, late_max_s=1.0)[0][1]
            time.sleep(max(0.0, start + 1.0 - time.time()))
            os.kill(os.getpid(), signal.SIGINT)

        with mock.patch.object(fakes.FakeTxDevice, "force_safe_state", spy):
            t = threading.Thread(target=interrupt_during_first_frame, daemon=True)
            t.start()
            code, events = run_cli(TX + ["--kind", "allcall", "--text", "JS8 SEQUENCE TEST DE DA2JH"])
            t.join()
        names = [e["event"] for e in events]
        self.assertEqual(names.count("keyed"), 1)
        self.assertIn("signal", names)
        self.assertIn("js8_cancelled", names)
        self.assertNotIn("js8_done", names)
        self.assertTrue(safe_calls)


class RxCliTests(unittest.TestCase):
    def test_rx_digimode_js8_emits_frames_and_message(self):
        from scipy import signal as sps
        from tests import fakes
        from pluto_advanced_rx import js8_rx
        rate = 1_000_000
        (frame, flags), = M.build_frames("DA2JH", "JO31", "DL1ABC SNR?", P.TURBO)
        usb, _ = js8.encode_frame_iq(frame, flags, P.TURBO, 1500.0, 48000, amplitude=0.3)
        x = np.zeros(int(6.5 * 48000), complex)
        x[int(0.1 * 48000):int(0.1 * 48000) + len(usb)] += usb
        iq = sps.resample_poly(x, 125, 6)
        rng = np.random.default_rng(1)
        iq = (iq + 0.02 * (rng.normal(size=len(iq)) + 1j * rng.normal(size=len(iq)))).astype(np.complex64)
        fakes.make_fake_rx(iq, rate)
        t0 = 1_000_000_002.0                                               # a 6 s period boundary

        class SampleClockReceiver(js8_rx.Js8Receiver):
            """The fake source is unpaced: time comes from the sample count; decode the one period."""

            def start(self):
                self._clock = lambda: t0 + self._written / 12000.0

                def once():
                    while self._written < 6 * 12000:
                        time.sleep(0.05)
                    from pluto_advanced_rx.js8_decoder import Js8SlotDecoder
                    dec = Js8SlotDecoder(self._backend)
                    self._on_decodes(t0, P.TURBO, dec.decode(self.slot_audio(t0, 6), P.TURBO))

                threading.Thread(target=once, daemon=True).start()
                return True

        with mock.patch.object(js8_rx, "Js8Receiver", SampleClockReceiver):
            code, events = run_cli(["rx", "ssb", "--device", "fake", "--uri", "x", "--freq", "144178000",
                                    "--bandwidth", "1000000", "--digimode", "js8", "--js8-submode", "turbo",
                                    "--js8-decoder", "own", "--duration", "25", "--json"])
        self.assertEqual(code, 0)
        frames = [e for e in events if e["event"] == "js8_frame"]
        self.assertEqual([(e["frame"], e["text"], e["speed"]) for e in frames],
                         [(frame, "DA2JH: DL1ABC SNR? ", "turbo")])
        msgs = [e for e in events if e["event"] == "js8_message"]
        self.assertEqual([(m["text"], m["sender"], m["to"], m["complete"]) for m in msgs],
                         [("DA2JH: DL1ABC SNR?", "DA2JH", "DL1ABC", True)])
        self.assertIn("js8_slot", [e["event"] for e in events])


if __name__ == "__main__":
    unittest.main()
