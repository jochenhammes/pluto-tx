"""J2: JS8 receive side -- decoders (pluto_advanced_rx/js8_decoder.py), multi-frame assembly (js8_assembly.py),
the receiver block and state (js8_rx.py) and the RX flowgraph branch.

Decode-rate thresholds (docs/JS8_PLAN.md J2 asks to set them from the J0 measurement with the reference
decoder). Measured 29.09.2026 on 20 slots per point, SNR in 2500 Hz (WSJT definition), random offset
200-2800 Hz, DT -1..+2 s, drift +-0.5 Hz over the frame:

    speed   own decoder (95 % down to)   JS8Call's decoder (tools/js8ref)
    NORMAL  -16 dB (80 % at -18)          -18 dB (25 % at -20)
    FAST    -14 dB (85 % at -16)          -12 dB (90 % at -14, 70 % at -16)
    TURBO   -12 dB (35 % at -14)          -10 dB (90 % at -12, 35 % at -14)
    SLOW    -20 dB (50 % at -22)          -18 dB (95 % at -20, 55 % at -22)

The plan's first guesses (NORMAL >= 95 % at -18 dB, SLOW at -22 dB) are beyond what JS8Call's own decoder
does under these conditions, so the tests pin the measured own-decoder values. 1000 noise-only slots
(250 per speed) gave 0 false decodes."""
import json
import os
import time
import unittest
import wave

import numpy as np

from pluto_advanced_rx import js8_decoder as D
from pluto_advanced_rx.js8_assembly import GAP_MARK, Js8Assembler
from pluto_tx import js8, js8_message as M, js8_phy as P

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WAVS = os.path.join(ROOT, "tests", "data", "js8", "wav")
RATE = 12000
TEXTS = ["DL1ABC SNR?", "CQ CQ CQ JO31", "DL1ABC SNR -12", "DL1ABC ACK"]
THRESHOLD_DB = {P.NORMAL: -16, P.FAST: -14, P.TURBO: -12, P.SLOW: -20}


def make_slot(sm, snr_db, seed, text=None, f0=None, dt=None, drift=None, frame_flags=None):
    rng = np.random.default_rng(seed)
    info = P.submode_info(sm)
    if frame_flags is None:
        frame_flags = M.build_frames("DA2JH", "JO31", text or TEXTS[seed % len(TEXTS)], sm)[0]
    frame, flags = frame_flags
    f0 = float(rng.uniform(200, 2800 - info["bandwidth_hz"])) if f0 is None else f0
    dt = float(rng.uniform(-1.0, 2.0)) if dt is None else dt
    drift = float(rng.uniform(-0.5, 0.5)) if drift is None else drift
    iq, dur = js8.encode_frame_iq(frame, flags, sm, f0, RATE, amplitude=1.0)
    t = np.arange(len(iq)) / RATE
    sig = (iq * np.exp(1j * np.pi * drift / dur * t ** 2)).real
    n = info["period_s"] * RATE
    x = np.zeros(n)
    s = int(round((info["start_delay_ms"] / 1000 + dt) * RATE))
    a, b = max(s, 0), min(s + len(sig), n)
    x[a:b] = sig[a - s:b - s]
    if snr_db is not None:
        x += rng.normal(0, np.sqrt(0.5 / 10 ** (snr_db / 10) * 6000 / 2500), n)
    return x, frame, flags, dt, f0


class OwnDecoderTests(unittest.TestCase):
    def test_decode_rate_at_measured_threshold(self):
        for sm, snr in THRESHOLD_DB.items():
            hits = 0
            for seed in range(20):
                x, frame, flags, _, _ = make_slot(sm, snr, seed)
                hits += any((d.frame, d.flags) == (frame, flags) for d in D.decode_own(x, sm))
            self.assertGreaterEqual(hits, 19, (P.SUBMODE_NAMES[sm], snr, hits))

    def test_estimates(self):
        for sm in (P.NORMAL, P.SLOW):
            x, frame, flags, dt, f0 = make_slot(sm, -12, 5, drift=0.0)
            (d,) = D.decode_own(x, sm)
            self.assertEqual((d.frame, d.flags), (frame, flags))
            self.assertAlmostEqual(d.snr_db, -12, delta=2.0)
            self.assertAlmostEqual(d.dt_s, dt, delta=0.1)
            self.assertAlmostEqual(d.freq_hz, f0, delta=P.submode_info(sm)["tone_spacing_hz"] / 2)
            self.assertEqual(d.text, M.decode_frame(frame, flags, sm)["message"])

    def test_two_signals_in_one_slot(self):
        sm = P.FAST
        a, fa, ba, _, _ = make_slot(sm, None, 1, text="CQ CQ CQ JO31", f0=800.0, dt=0.0)
        b, fb, bb, _, _ = make_slot(sm, None, 2, text="DL1ABC ACK", f0=1600.0, dt=0.5)
        got = D.decode_own(a + b + np.random.default_rng(3).normal(0, 0.3, len(a)), sm)
        self.assertEqual({(d.frame, d.flags) for d in got}, {(fa, ba), (fb, bb)})

    def test_no_decodes_from_noise(self):
        rng = np.random.default_rng(99)
        for sm in P.SUBMODES:
            for _ in range(10):
                self.assertEqual(D.decode_own(rng.normal(0, 1, P.SUBMODES[sm]["period_s"] * RATE), sm), [])

    def test_reference_wavs(self):
        with open(os.path.join(WAVS, "decodes.jsonl")) as f:
            refs = [json.loads(line) for line in f]
        for ref in refs:
            with wave.open(os.path.join(WAVS, ref["file"])) as w:
                x = np.frombuffer(w.readframes(w.getnframes()), "<i2").astype(float)
            got = D.decode_own(x, ref["submode"])
            self.assertEqual([(d.frame, d.flags, d.text) for d in got], [(ref["frame"], ref["bits"], ref["message"])])

    def test_bp_decoder_corrects_errors(self):
        msg = P.frame_to_bits("QpYbXBUUeS00", 3)
        cw = np.concatenate([P.parity_bits(msg), msg])
        llr = np.where(cw == 1, 4.0, -4.0)
        rng = np.random.default_rng(0)
        flip = rng.choice(174, 12, replace=False)
        llr[flip] *= -0.5                                    # 12 wrong but weak bits
        plain, err = D.bp_decode(llr)
        self.assertEqual(err, 0)
        self.assertTrue(np.array_equal(plain, cw))


@unittest.skipUnless("js8" in D.available_backends(), "tools/js8ref not built (install-js8.sh)")
class Js8RefBackendTests(unittest.TestCase):
    def test_slot_decoder_js8(self):
        dec = D.Js8SlotDecoder("js8")
        x, frame, flags, dt, f0 = make_slot(P.TURBO, -8, 4, drift=0.0)
        (d,) = dec.decode(x, P.TURBO)
        dec.close()
        self.assertEqual((d.frame, d.flags, d.submode), (frame, flags, P.TURBO))
        self.assertAlmostEqual(d.dt_s, dt, delta=0.1)

    def test_parse_output(self):
        out = ('{"bits":3,"dt":0.1,"frame":"QpYbXBUUeS00","frame_type":3,"freq":1234.5,'
               '"message":"x","quality":1,"snr":-7,"submode":0}\n{"finished":1,"ms":2400}\nnoise\n')
        (d,) = D.parse_js8ref_output(out)
        self.assertEqual((d.frame, d.flags, d.snr_db, d.freq_hz), ("QpYbXBUUeS00", 3, -7.0, 1234.5))
        self.assertEqual(d.text, "DA2JH: DL1ABC SNR? ")


def decodes_for(text, sm, f0, first_slot=0.0):
    """-> [(slot, [Js8Decode])] of a whole transmission as a receiver would get it."""
    frames = M.build_frames("DA2JH", "JO31", text, sm)
    period = P.SUBMODES[sm]["period_s"]
    out = []
    for i, (f, b) in enumerate(frames):
        d = M.decode_frame(f, b, sm)
        out.append((first_slot + i * period,
                    [D.Js8Decode(f, b, sm, -10.0, 0.0, f0, d["message"], d["frame_type"])]))
    return out


class AssemblyTests(unittest.TestCase):
    TEXT = "@ALLCALL THIS IS A TEST OF A LONGER MESSAGE 73"

    def test_three_frames_one_message(self):
        seq = decodes_for(self.TEXT, P.FAST, 1000.0)
        self.assertGreaterEqual(len(seq), 3)
        asm = Js8Assembler()
        for slot, decs in seq:
            changed = asm.feed(slot, P.FAST, decs)
        (msg,) = changed
        self.assertTrue(msg.closed and msg.complete and not msg.incomplete)
        self.assertEqual(msg.text, ("DA2JH: @ALLCALL  " + self.TEXT[len("@ALLCALL "):]).strip())
        self.assertEqual((msg.sender, msg.to), ("DA2JH", "@ALLCALL"))
        self.assertIn("DA2JH", asm.stations)

    def test_missing_middle_frame_marked_incomplete(self):
        seq = decodes_for(self.TEXT, P.FAST, 1000.0)
        del seq[1]
        asm = Js8Assembler()
        for slot, decs in seq:
            changed = asm.feed(slot, P.FAST, decs)
        (msg,) = changed
        self.assertTrue(msg.closed and msg.incomplete)
        self.assertIn(GAP_MARK.strip(), msg.text)

    def test_timeout_without_last_frame(self):
        seq = decodes_for(self.TEXT, P.NORMAL, 1000.0)[:2]
        asm = Js8Assembler()
        for slot, decs in seq:
            asm.feed(slot, P.NORMAL, decs)
        self.assertEqual(asm.expire(seq[-1][0] + 15 + 15), [])          # one missing period: still open
        (msg,) = asm.expire(seq[-1][0] + 15 + 30)                          # two missing periods: closed
        self.assertTrue(msg.incomplete and msg.text.endswith(GAP_MARK.strip()))

    def test_two_stations_on_different_offsets(self):
        a = decodes_for(self.TEXT, P.NORMAL, 1000.0)
        b = decodes_for("DL1ABC SNR -12", P.NORMAL, 1040.0, first_slot=15.0)
        asm = Js8Assembler()
        slots = {}
        for slot, decs in a + b:
            slots.setdefault(slot, []).extend(decs)
        done = []
        for slot in sorted(slots):
            done += [m for m in asm.feed(slot, P.NORMAL, slots[slot]) if m.closed]
        self.assertEqual(len(done), 2)
        texts = sorted(m.text for m in done)
        self.assertEqual(texts[0], "DA2JH: @ALLCALL  THIS IS A TEST OF A LONGER MESSAGE 73")
        self.assertEqual(texts[1], "DA2JH: DL1ABC SNR -12")


class ReceiverTests(unittest.TestCase):
    def test_receiver_decodes_each_speed_at_shared_boundary(self):
        from pluto_advanced_rx.js8_rx import Js8Receiver, Js8State
        state = Js8State()
        t0 = 1_000_000_020.0                                     # a :00/:30 boundary for 15 s and 30 s
        slow, fs, bs, _, _ = make_slot(P.SLOW, None, 1, text="CQ CQ CQ JO31", f0=700.0, dt=0.0)
        normal, fn, bn, _, _ = make_slot(P.NORMAL, None, 2, text="DL1ABC ACK", f0=1500.0, dt=0.0)
        audio = slow.copy()
        audio[15 * RATE:30 * RATE] += normal
        fake_now = [t0 + 30.0]
        recv = Js8Receiver(state.on_decodes, submodes=(P.NORMAL, P.SLOW), backend="own",
                           clock=lambda: fake_now[0])
        recv.work([audio.astype(np.float32)], [])
        fake_now[0] = t0 + 29.9                                  # both periods end at t0 + 30
        recv.start()
        for _ in range(400):
            if state.slots >= 2:
                break
            time.sleep(0.05)
        recv.stop()
        snap = state.get_snapshot()
        got = {(r["submode"], r["frame"]) for r in snap["rows"]}
        self.assertEqual(got, {(P.SLOW, fs), (P.NORMAL, fn)})
        self.assertEqual({m["text"] for m in snap["messages"]},
                         {"DA2JH: @ALLCALL CQ CQ CQ JO31", "DA2JH: DL1ABC ACK"})
        self.assertEqual(snap["stations"][0]["call"], "DA2JH")

    def test_rx_flowgraph_branch(self):
        from scipy import signal
        from tests import fakes
        from pluto_advanced_rx import flowgraph as rxf
        rate = 1_000_000
        (frame, flags), = M.build_frames("DA2JH", "JO31", "DL1ABC SNR?", P.TURBO)
        usb, _ = js8.encode_frame_iq(frame, flags, P.TURBO, 1500.0, 48000, amplitude=0.3)
        x = np.zeros(int(6.5 * 48000), complex)
        x[int(0.1 * 48000):int(0.1 * 48000) + len(usb)] += usb
        iq = signal.resample_poly(x, 125, 6)
        rng = np.random.default_rng(1)
        iq = (iq + 0.02 * (rng.normal(size=len(iq)) + 1j * rng.normal(size=len(iq)))).astype(np.complex64)
        fakes.make_fake_rx(iq, rate)
        rx = rxf.AdvancedRxFlowgraph(uri="x", frequency=144_178_000.0, sample_rate=rate, device_type="fake",
                                     active_digimode="js8", js8_submodes=(P.TURBO,), js8_backend="own")
        t0 = 1_000_000_002.0                                     # a 6 s period boundary
        recv = rx.js8_receiver
        recv._clock = lambda: t0 + recv._written / 12000.0       # sample-driven clock
        recv.start = lambda: True                                # no decode thread here
        rx.start()
        rx.wait()
        got = D.decode_own(recv.slot_audio(t0, 6), P.TURBO)
        self.assertEqual([(d.frame, d.text) for d in got], [(frame, "DA2JH: DL1ABC SNR? ")])
        self.assertAlmostEqual(got[0].dt_s, 0.0, delta=0.2)


if __name__ == "__main__":
    unittest.main()
