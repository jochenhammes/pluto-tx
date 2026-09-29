"""J1: JS8 waveform (pluto_tx/js8.py): tones come back out of the ideal signal, the occupied bandwidth is
8 x tone spacing, slot planning, and -- when tools/js8ref is built -- JS8Call's own decoder reads it."""
import os
import subprocess
import tempfile
import unittest
import wave

import numpy as np

from pluto_tx import js8, js8_message as M, js8_phy as P

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JS8REF = os.environ.get("JS8REF", os.path.join(ROOT, "js8call", "build-js8ref", "js8ref"))
RATE = 12000


def demod_tones(iq, submode, tone_hz, rate=RATE):
    info = P.submode_info(submode)
    n = int(round(info["symbol_period_s"] * rate))
    t = np.arange(n) / rate
    tones = []
    for k in range(P.NUM_SYMBOLS):
        seg = iq[k * n:(k + 1) * n]
        powers = [abs(np.sum(seg * np.exp(-2j * np.pi * (tone_hz + j * info["tone_spacing_hz"]) * t)))
                  for j in range(8)]
        tones.append(int(np.argmax(powers)))
    return tones


class WaveformTests(unittest.TestCase):
    def test_tones_recovered_from_ideal_signal(self):
        for sm in (P.NORMAL, P.FAST, P.TURBO, P.SLOW):
            iq, dur = js8.encode_frame_iq("QpYbXBUUeS00", 3, sm, 1000.0, RATE)
            self.assertAlmostEqual(dur, P.submode_info(sm)["data_duration_s"])
            self.assertEqual(demod_tones(iq, sm, 1000.0), list(P.encode("QpYbXBUUeS00", 3, sm)))

    def test_occupied_bandwidth(self):
        for sm, bw in ((P.NORMAL, 50.0), (P.FAST, 80.0), (P.TURBO, 160.0), (P.SLOW, 25.0)):
            iq, _ = js8.encode_frame_iq("1evxkMBjPUWe", 3, sm, 1000.0, RATE)
            spec = np.abs(np.fft.fft(iq, 1 << 20)) ** 2
            f = np.fft.fftfreq(1 << 20, 1 / RATE)
            order = np.argsort(f)
            f, spec = f[order], spec[order]
            c = np.cumsum(spec) / spec.sum()
            lo, hi = f[np.searchsorted(c, 0.005)], f[np.searchsorted(c, 0.995)]
            self.assertGreater(lo, 1000.0 - 0.3 * bw, sm)          # 99 % of the power inside the 8 tones
            self.assertLess(hi, 1000.0 + 1.3 * bw, sm)
            self.assertGreater(hi - lo, 0.7 * bw, sm)

    def test_continuous_phase_and_amplitude(self):
        iq, _ = js8.encode_frame_iq("QpYbXBUUeS00", 3, P.FAST, 1500.0, 48000, amplitude=0.5)
        mid = iq[1000:-1000]
        self.assertTrue(np.allclose(np.abs(mid), 0.5, atol=1e-4))
        dphi = np.angle(mid[1:] * np.conj(mid[:-1]))
        self.assertLess(np.max(np.abs(dphi)), 2 * np.pi * (1500 + 8 * 10) / 48000 + 1e-3)


class PlanTests(unittest.TestCase):
    def test_consecutive_periods(self):
        plan = js8.plan_frames(1000.2, P.NORMAL, 3)
        self.assertEqual([s for _, s in plan], [1005.5, 1020.5, 1035.5])
        self.assertEqual([s - k for k, s in plan], [3.0, 0.5, 0.5])        # one swap, then only re-keying
        plan = js8.plan_frames(990.9, P.TURBO, 2, key_early_s=1.0)     # current period still usable
        self.assertEqual([s for _, s in plan], [990.9, 996.1])

    def test_limits(self):
        with self.assertRaises(ValueError):
            js8.plan_frames(0.0, P.NORMAL, 21)
        with self.assertRaises(ValueError):
            js8.plan_frames(0.0, P.NORMAL, 0)
        self.assertAlmostEqual(js8.transmission_time_s(1, P.NORMAL), 0.5 + 12.64)


@unittest.skipUnless(os.access(JS8REF, os.X_OK), "tools/js8ref not built (install-js8.sh)")
class Js8CallDecodesUsTests(unittest.TestCase):
    """The strongest check there is without a radio: JS8Call's own decoder reads our signal."""

    def test_reference_decoder_reads_all_speeds(self):
        text = M.compose("snr_reply", "DA2JH", "JO31", to="DL1ABC", snr_db=-12)
        for sm, mask in ((P.NORMAL, 1), (P.FAST, 2), (P.TURBO, 4), (P.SLOW, 8)):
            (frame, flags), = M.build_frames("DA2JH", "JO31", text, sm)
            info = P.submode_info(sm)
            iq, _ = js8.encode_frame_iq(frame, flags, sm, 1200.0, RATE)
            x = np.zeros(info["period_s"] * RATE)
            s = int(info["start_delay_ms"] / 1000 * RATE)
            x[s:s + len(iq)] = iq.real
            with tempfile.TemporaryDirectory() as tmp:
                path = os.path.join(tmp, "slot.wav")
                with wave.open(path, "wb") as w:
                    w.setnchannels(1)
                    w.setsampwidth(2)
                    w.setframerate(RATE)
                    w.writeframes((x / np.max(np.abs(x)) * 20000).astype("<i2").tobytes())
                out = subprocess.run([JS8REF, "decode", path, str(mask)], capture_output=True, text=True,
                                     timeout=60).stdout
            self.assertIn('"frame":"%s"' % frame, out, sm)
            self.assertIn("DA2JH: DL1ABC SNR -12", out, sm)


if __name__ == "__main__":
    unittest.main()
