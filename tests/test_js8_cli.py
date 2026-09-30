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

    def setUp(self):
        import tempfile
        from pluto_cli import runtime
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.state_path = os.path.join(tmp.name, "js8_drift.json")
        patcher = mock.patch.object(runtime, "JS8_DRIFT_STATE_PATH", self.state_path)  # never the user's file
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_drift_state_across_runs(self):
        from pluto_cli import runtime

        class Tb:
            history = None

            def restore_drift_history(self, running_s, since_last_tx_s):
                self.history = (running_s, since_last_tx_s)

        runtime.js8_drift_save("pluto:ip:x", 1000.0, 1100.0)
        tb = Tb()
        self.assertEqual(runtime.js8_drift_restore(tb, "pluto:ip:x", now=1160.0), 1000.0)   # continues
        self.assertEqual(tb.history, (160.0, 60.0))
        tb = Tb()
        late = 1100.0 + runtime.JS8_DRIFT_SESSION_GAP_S + 1
        self.assertEqual(runtime.js8_drift_restore(tb, "pluto:ip:x", now=late), late)          # cold again
        self.assertIsNone(tb.history)
        self.assertEqual(runtime.js8_drift_restore(tb, "hackrf:", now=1160.0), 1160.0)       # other device
        with open(self.state_path, "w") as f:
            f.write("{broken")
        self.assertEqual(runtime.js8_drift_restore(tb, "pluto:ip:x", now=1160.0), 1160.0)
        self.assertIsNone(tb.history)

    def test_cli_runs_record_and_continue_the_session(self):
        code, _events = run_cli(TX + ["--kind", "cq"])
        self.assertEqual(code, 0)
        with open(self.state_path) as f:
            (first,) = json.load(f).values()
        self.assertLessEqual(first["session_start"], first["last_tx_end"])
        self.assertLess(time.time() - first["last_tx_end"], 5.0)
        code, _events = run_cli(TX + ["--kind", "hb"])                     # restores into a real flowgraph
        self.assertEqual(code, 0)
        with open(self.state_path) as f:
            (second,) = json.load(f).values()
        self.assertEqual(second["session_start"], first["session_start"])   # the same session
        self.assertGreater(second["last_tx_end"], first["last_tx_end"])

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

        from pluto_tx.flowgraph import PlutoTxFlowgraph
        keyed = threading.Event()
        frame_start = []
        real_key = PlutoTxFlowgraph.key_ptt

        def key_and_note(fg):
            real_key(fg)
            frame_start.append(fg.js8_frame_starts[fg.js8_frame_index])
            keyed.set()

        def interrupt_during_first_frame():
            keyed.wait(30)                                                   # the first frame is keyed ...
            time.sleep(max(0.0, frame_start[0] + 1.0 - time.time()))        # ... interrupt 1 s into it
            os.kill(os.getpid(), signal.SIGINT)

        with mock.patch.object(fakes.FakeTxDevice, "force_safe_state", spy), \
                mock.patch.object(PlutoTxFlowgraph, "key_ptt", key_and_note):
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
