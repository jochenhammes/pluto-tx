"""J3: JS8 transmit branch of PlutoTxFlowgraph with a fake TX device (captures everything sent to the sink):
timing, one keying per frame in consecutive periods, abort paths, band check. The capture is decoded with
the J2 decoder."""
import time
import unittest
from unittest import mock

import numpy as np
from scipy import signal

from pluto_advanced_rx import js8_decoder as D
from pluto_tx import config, js8, js8_message as M, js8_phy as P
from pluto_tx import flowgraph as txf

JS8 = txf.PlutoTxFlowgraph.MODE_JS8
SINK_RATE = 2_500_000
TEXT = "@ALLCALL JS8 SEQUENCE TEST DE DA2JH"


def bursts(iq, thr=0.05, min_gap_s=0.5):
    """(start, end) sample indices of the keyed stretches in a capture."""
    on = np.flatnonzero(np.abs(iq) > thr)
    if not len(on):
        return []
    cuts = np.flatnonzero(np.diff(on) > min_gap_s * SINK_RATE)
    starts = np.concatenate([[on[0]], on[cuts + 1]])
    ends = np.concatenate([on[cuts], [on[-1]]])
    return list(zip(starts, ends))


def decode_burst(iq, a, b, submode):
    bb = signal.resample_poly(iq[a:b], 3, 625)                     # 12 kHz complex baseband
    info = P.submode_info(submode)
    lead = int(info["start_delay_ms"] / 1000 * 12000)
    audio = np.concatenate([np.zeros(lead), np.real(bb)])
    audio = audio + np.random.default_rng(0).normal(0, 0.05, len(audio))
    return D.decode_own(audio, submode)


class Js8TxFlowgraphTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from tests import fakes
        fakes.register_tx()
        fakes.register_fake_audio_tx()

    def make(self, frames, submode=P.TURBO, frequency=144_178_000.0, device_type="fake"):
        fg = txf.PlutoTxFlowgraph(device_type=device_type, mode=JS8, frequency=frequency,
                                  js8_frames=frames, js8_submode=submode, js8_tone_hz=1500.0)
        fg.start()
        self.addCleanup(lambda: (fg.stop(), fg.wait()))
        time.sleep(0.3)
        return fg

    def run_series(self, fg, submode, n, stop_after=None, on_frame=None):
        """Key/unkey like the CLI/GUI series will (js8.plan_frames). stop_after: cancel after that many
        frames. on_frame(i): called while frame i is keyed. Returns the plan."""
        # always an on-time start (full key-early lead, no late start into a running period)
        plan = js8.plan_frames(time.time() + config.JS8_KEY_EARLY_S + 0.3, submode, n,
                               key_early_s=config.JS8_KEY_EARLY_S, late_max_s=0.0,
                               rekey_early_s=config.JS8_REKEY_EARLY_S)
        fg.js8_start_at = plan[0][1]
        for i, (key_at, start) in enumerate(plan):
            if stop_after is not None and i >= stop_after:
                fg.js8_cancel()
                break
            while time.time() < key_at:
                time.sleep(0.01)
            fg.key_ptt()
            if on_frame is not None and on_frame(i):
                return plan
            # + slack: under heavy host load the unpaced fake chain can trail the wall clock a little
            # (a real device paces the stream); still far ahead of the next frame's re-key
            time.sleep(fg.js8_hold_s + 0.3)
            fg.unkey_ptt()
        return plan

    def test_single_frame_timing_and_duration(self):
        frames = M.build_frames("DA2JH", "JO31", "DL1ABC SNR?", P.TURBO)
        fg = self.make(frames)
        self.assertIsNone(fg.js8_problem())
        plan = self.run_series(fg, P.TURBO, 1)
        start = plan[0][1]
        self.assertAlmostEqual(fg.js8_source.frame_started_at[0], start - config.JS8_TX_LATENCY_S, delta=0.06)
        self.assertEqual(fg.js8_source.frames_sent, {0})
        self.assertFalse(fg.js8_active)
        iq = np.array(fg.device.sink.data(), dtype=np.complex64)
        (a, b), = bursts(iq)
        self.assertAlmostEqual((b - a) / SINK_RATE, 79 * 0.05, delta=0.02)
        got = decode_burst(iq, a, b, P.TURBO)
        self.assertEqual([(d.frame, d.flags) for d in got], frames)

    def test_three_frames_in_consecutive_periods_dark_in_between(self):
        frames = M.build_frames("DA2JH", "JO31", TEXT, P.TURBO)
        self.assertEqual(len(frames), 3)
        fg = self.make(frames)
        with mock.patch.object(fg.device, "pre_key", wraps=fg.device.pre_key) as pre, \
                mock.patch.object(fg.device, "post_unkey", wraps=fg.device.post_unkey) as post:
            plan = self.run_series(fg, P.TURBO, 3)
        self.assertEqual((pre.call_count, post.call_count), (3, 3))          # one keying per frame
        self.assertEqual([s - plan[0][1] for _, s in plan], [0.0, 6.0, 12.0])
        iq = np.array(fg.device.sink.data(), dtype=np.complex64)
        bs = bursts(iq)
        self.assertEqual(len(bs), 3)
        gaps = [(bs[i + 1][0] - bs[i][0]) / SINK_RATE for i in range(2)]
        for g in gaps:
            self.assertAlmostEqual(g, 6.0, delta=0.002)                      # sample-exact periods
        for (a, b), frame in zip(bs, frames):
            self.assertEqual([(d.frame, d.flags) for d in decode_burst(iq, a, b, P.TURBO)], [frame])
            self.assertAlmostEqual((b - a) / SINK_RATE, 3.95, delta=0.02)
        between = iq[bs[0][1] + SINK_RATE // 10:bs[1][0] - SINK_RATE // 10]
        self.assertEqual(float(np.max(np.abs(between))), 0.0)               # nothing between frames
        self.assertFalse(fg.js8_active)
        self.assertEqual(M.frames_text([(d.frame, d.flags) for a, b in bs for d in decode_burst(iq, a, b, P.TURBO)],
                                       P.TURBO), "DA2JH: @ALLCALL  JS8 SEQUENCE TEST DE DA2JH")

    def test_cancel_after_first_frame_sends_nothing_more(self):
        frames = M.build_frames("DA2JH", "JO31", TEXT, P.TURBO)
        fg = self.make(frames)
        self.run_series(fg, P.TURBO, 3, stop_after=1)
        time.sleep(7.0)                                                      # past where frame 2 was due
        self.assertEqual(set(fg.js8_source.frame_started_at), {0})           # frame 1 never began
        self.assertTrue(fg.js8_source.cancelled)
        self.assertEqual(len(bursts(np.array(fg.device.sink.data(), dtype=np.complex64))), 1)
        with self.assertRaisesRegex(ValueError, "no slot planned"):
            fg.key_ptt()                                                     # the old run is gone for good
        self.assertFalse(fg.keyed)

    def test_estop_during_a_frame(self):
        frames = M.build_frames("DA2JH", "JO31", TEXT, P.TURBO)
        fg = self.make(frames)

        def estop(i):
            time.sleep(max(0.0, fg.js8_frame_starts[0] + 1.0 - time.time()))  # 1 s into frame 0
            fg.unkey_ptt()                                                   # what the GUI's E-STOP does
            fg.device.force_safe_state()
            return True

        with mock.patch.object(fg.device, "force_safe_state", wraps=fg.device.force_safe_state) as safe:
            self.run_series(fg, P.TURBO, 3, on_frame=estop)
            self.assertEqual(safe.call_count, 1)
        t_stop = time.time()
        self.assertFalse(fg.keyed)
        self.assertTrue(fg.js8_source.cancelled)
        time.sleep(8.0)
        iq = np.array(fg.device.sink.data(), dtype=np.complex64)
        (a, b), = bursts(iq)
        self.assertLess((b - a) / SINK_RATE, 3.95 - 1.5)                    # cut short, nothing after
        self.assertEqual(set(fg.js8_source.frame_started_at), {0})
        self.assertNotIn(0, fg.js8_source.frames_sent)
        self.assertLess(time.time() - t_stop, 30)

    def test_audio_device_keys_each_frame(self):
        frames = M.build_frames("DA2JH", "JO31", TEXT, P.TURBO)
        fg = self.make(frames, device_type="fake_audio")
        self.assertIsNone(fg.js8_problem())                                  # no band check without RF
        pre, post = fg.device.pre_key_calls, fg.device.post_unkey_calls     # construction idles the device
        self.run_series(fg, P.TURBO, 3)
        self.assertEqual((fg.device.pre_key_calls - pre, fg.device.post_unkey_calls - post), (3, 3))
        self.assertEqual(fg.js8_source.frames_sent, {0, 1, 2})

    def test_problem_reports_and_band_edges(self):
        frames = M.build_frames("DA2JH", "JO31", "CQ CQ CQ JO31", P.NORMAL)
        fg = txf.PlutoTxFlowgraph(device_type="fake", mode=JS8, frequency=144_178_000.0,
                                  js8_frames=frames, js8_submode=P.NORMAL, js8_tone_hz=1500.0)
        self.assertIsNone(fg.js8_problem())
        self.assertEqual(fg.js8_occupied_hz(), (144_179_500.0, 144_179_550.0))
        for freq, ok in ((7_078_000.0, True), (14_078_000.0, True), (100_000_000.0, False),
                         (7_198_460.0, False),                        # tone 0 inside 40 m, the top tone above
                         (7_198_400.0, True), (13_998_000.0, False), (145_999_000.0, False)):
            fg.set_frequency(freq)
            self.assertEqual(fg.js8_problem() is None, ok, freq)
            if not ok:
                self.assertIn("amateur band", fg.js8_problem())
        fg.set_frequency(144_178_000.0)
        fg.set_js8_tone_hz(2990.0)
        self.assertIn("audio offset", fg.js8_problem())
        fg.set_js8_tone_hz(1500.0)
        fg.set_js8_message([])
        self.assertIn("no message", fg.js8_problem())
        fg.set_js8_message([("QpYbXBUUeS00", 3)] * 21)
        self.assertIn("too long", fg.js8_problem())
        fg.set_js8_message(frames)
        with self.assertRaisesRegex(ValueError, "no slot planned"):
            fg.key_ptt()                                                     # refused before RF
        self.assertFalse(fg.keyed)
        fg.set_frequency(100_000_000.0)
        fg.js8_start_at = time.time() + 5
        with self.assertRaisesRegex(ValueError, "amateur band"):
            fg.key_ptt()
        self.assertFalse(fg.keyed)
        self.assertEqual(config.in_amateur_band(7_100_000), "40m")
        self.assertEqual(config.in_amateur_band(14_200_000), "20m")


if __name__ == "__main__":
    unittest.main()
