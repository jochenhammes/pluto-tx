"""J1: JS8 physical layer (pluto_tx/js8_phy.py) against docs/js8/SPEC.md 1.x and the JS8Call reference vectors."""
import json
import os
import unittest

import numpy as np

from pluto_tx import js8_phy as P

DATA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "js8")


def load_vectors():
    with open(os.path.join(DATA, "vectors.jsonl")) as f:
        return [json.loads(line) for line in f]


class CrcTests(unittest.TestCase):
    def test_crc12_of_reference_frames(self):
        # the CRC sits in message bits 75..86; the message symbols of the reference tones carry it
        for case in load_vectors()[:40]:
            for fr in case["frames"]:
                tones = [int(t) for t in fr["tones"]]
                msg = P.codeword_from_tones(tones)[P.K:]
                self.assertTrue(np.array_equal(P.frame_to_bits(fr["frame"], fr["bits"]), msg))

    def test_crc12_detects_bit_errors(self):
        msg = P.frame_to_bits("QpYbXBUUeS00", 3)
        self.assertEqual(P.bits_to_frame(msg), ("QpYbXBUUeS00", 3))
        for i in range(P.K):
            bad = msg.copy()
            bad[i] ^= 1
            self.assertIsNone(P.bits_to_frame(bad), i)

    def test_crc12_known_value(self):
        # all-zero 75-bit message: the augmented CRC of zeros is 0, so the sent CRC is the final XOR (42)
        self.assertEqual(P.crc12(bytes(11)), 42)


class EncodeTests(unittest.TestCase):
    def test_tones_match_reference_vectors(self):
        n = 0
        for case in load_vectors():
            for fr in case["frames"]:
                tones = "".join(str(t) for t in P.encode(fr["frame"], fr["bits"], case["submode"]))
                self.assertEqual(tones, fr["tones"], (case["case"], fr["frame"]))
                n += 1
        self.assertGreater(n, 250)

    def test_codewords_satisfy_parity_checks(self):
        h = P.check_matrix()
        rng = np.random.default_rng(3)
        for _ in range(30):
            frame = "".join(rng.choice(list(P.T.PHY_ALPHABET), 12))
            tones = P.encode(frame, int(rng.integers(0, 8)), P.NORMAL)
            self.assertFalse(np.any((h @ P.codeword_from_tones(tones)) % 2))

    def test_bits_tones_roundtrip(self):
        for flags in range(8):
            for sm in P.SUBMODES:
                tones = P.encode("0Az-+9abcXYZ", flags, sm)
                self.assertEqual(P.decode_tones(tones), ("0Az-+9abcXYZ", flags))
                self.assertTrue(all(0 <= t <= 7 for t in tones))

    def test_costas_positions(self):
        for sm in P.SUBMODES:
            tones = P.encode("QpYbXBUUeS00", 3, sm)
            self.assertTrue(P.costas_ok(tones, sm))
            arrays = P.costas(sm)
            for pos, arr in zip((0, 36, 72), arrays):
                self.assertEqual(tuple(tones[pos:pos + 7]), tuple(arr))
        self.assertEqual(P.costas(P.NORMAL)[0], (4, 2, 5, 6, 1, 3, 0))
        self.assertEqual(P.costas(P.FAST)[1], (1, 5, 0, 2, 3, 6, 4))

    def test_rejects_bad_frames(self):
        with self.assertRaises(ValueError):
            P.encode("SHORT", 0, P.NORMAL)
        with self.assertRaises(ValueError):
            P.encode("QpYbXBUUeS0!", 0, P.NORMAL)
        tones = P.encode("QpYbXBUUeS00", 3, P.NORMAL)
        tones[50] = (tones[50] + 1) % 8
        self.assertIsNone(P.decode_tones(tones))


class SubmodeTests(unittest.TestCase):
    def test_durations_and_spacing(self):
        want = {P.NORMAL: (0.16, 6.25, 15), P.FAST: (0.10, 10.0, 10), P.TURBO: (0.05, 20.0, 6),
                P.SLOW: (0.32, 3.125, 30)}
        for sm, (sym, spacing, period) in want.items():
            info = P.submode_info(sm)
            self.assertAlmostEqual(info["symbol_period_s"], sym)
            self.assertAlmostEqual(info["tone_spacing_hz"], spacing)
            self.assertAlmostEqual(info["bandwidth_hz"], 8 * spacing)
            self.assertAlmostEqual(info["data_duration_s"], 79 * sym)
            self.assertEqual(info["period_s"], period)


if __name__ == "__main__":
    unittest.main()
