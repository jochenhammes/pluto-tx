"""POCSAG codec, encoder and decoder maths (no GNU Radio needed)."""
import random
import unittest

import numpy as np

from pluto_tx import pocsag, pocsag_codec as c


def decode_bits(bits, flip=0, error_rate=0.0, seed=1):
    out = []
    dec = c.BatchDecoder(out.append)
    rng = random.Random(seed)
    for b in bits:
        dec.push_bit(b ^ flip ^ (1 if rng.random() < error_rate else 0))
    dec.flush()
    return out, dec


class CodewordTests(unittest.TestCase):
    def test_sync_and_idle_are_valid_codewords(self):
        self.assertTrue(c.is_valid(c.SYNC))
        self.assertTrue(c.is_valid(c.IDLE))

    def test_address_and_message_codewords_layout(self):
        cw = c.address_codeword(0x2AAAA, 2)
        self.assertEqual(cw >> 31, 0)
        self.assertEqual((cw >> 13) & 0x3FFFF, 0x2AAAA)
        self.assertEqual((cw >> 11) & 3, 2)
        self.assertTrue(c.is_valid(cw))
        mw = c.message_codeword(0xABCDE)
        self.assertEqual(mw >> 31, 1)
        self.assertEqual((mw >> 11) & 0xFFFFF, 0xABCDE)
        self.assertTrue(c.is_valid(mw))

    def test_bch_corrects_up_to_two_errors_and_rejects_three(self):
        rng = random.Random(7)
        for _ in range(300):
            cw = c.message_codeword(rng.getrandbits(20))
            for n in (0, 1, 2):
                bad = cw
                for pos in rng.sample(range(32), n):
                    bad ^= 1 << pos
                fixed = c.correct_codeword(bad)
                self.assertIsNotNone(fixed)
                self.assertEqual(fixed, (cw, n))
        misses = 0
        for _ in range(300):
            cw = c.message_codeword(rng.getrandbits(20))
            bad = cw
            for pos in rng.sample(range(32), 3):
                bad ^= 1 << pos
            fixed = c.correct_codeword(bad)
            if fixed is not None:
                self.assertNotEqual(fixed[0], bad)   # never returns the corrupted word as "clean"
                misses += 1
        self.assertLess(misses, 60)                  # a 3-error word may alias to a neighbour, but rarely


class TextCodingTests(unittest.TestCase):
    def test_alpha_is_7_bit_lsb_first_and_padded_to_codewords(self):
        bits = c.text_to_bits("A")                    # 0x41 = 1000001 -> LSB first 1,0,0,0,0,0,1
        self.assertEqual(bits[:7], [1, 0, 0, 0, 0, 0, 1])
        self.assertEqual(bits[7:14], [0, 0, 1, 0, 0, 0, 0])   # EOT 0x04 LSB first
        self.assertEqual(len(bits) % 20, 0)

    def test_numeric_bcd_and_padding(self):
        bits = c.text_to_bits("12", "numeric")
        self.assertEqual(bits[:8], [1, 0, 0, 0, 0, 1, 0, 0])  # 1 then 2, LSB first
        self.assertEqual(c.bits_to_text(bits, "numeric"), "12")
        self.assertEqual(len(bits) % 20, 0)

    def test_german_charset_roundtrip_and_ascii_replacement(self):
        text = "Grüße ÄÖÜäöüß"
        bits = c.text_to_bits(text, "alpha", "de")
        self.assertEqual(c.bits_to_text(bits, "alpha", "de"), text)
        self.assertEqual(c.bits_to_text(c.text_to_bits("a[b", "alpha", "de"), "alpha", "de"), "a?b")
        self.assertEqual(c.bits_to_text(c.text_to_bits("Grüße", "alpha", "ascii"), "alpha", "ascii"), "Gr??e")


class TransmissionTests(unittest.TestCase):
    def test_structure(self):
        bits = c.build_transmission(1234567, 3, c.text_to_bits("hi"))
        self.assertEqual(bits[:576], [1, 0] * 288)
        sync = [(c.SYNC >> (31 - i)) & 1 for i in range(32)]
        self.assertEqual(bits[576:608], sync)
        self.assertEqual((len(bits) - 576) % (32 * 17), 0)

    def test_roundtrip_all_frames_and_kinds(self):
        for ric in list(range(1000, 1008)) + [0, 7, 8, c.RIC_MAX]:
            for kind, text, fn in (("alpha", "Test %d" % ric, 3), ("numeric", "0123456789-U[]* ", 1), ("tone", "", 1)):
                payload = None if kind == "tone" else c.text_to_bits(text, kind)
                out, dec = decode_bits(c.build_transmission(ric, fn, payload))
                self.assertEqual(len(out), 1, (ric, kind))
                m = out[0]
                self.assertEqual((m["ric"], m["function"]), (ric, fn))
                if kind == "tone":
                    self.assertEqual(m["payload"], "")
                else:
                    self.assertEqual(c.decode_message(m, "alpha" if kind == "alpha" else "numeric"), text.rstrip())

    def test_long_message_spans_batches(self):
        text = "The quick brown fox jumps over the lazy dog 0123456789 " * 2
        bits = c.build_transmission(1234567, 3, c.text_to_bits(text))
        out, dec = decode_bits(bits)
        self.assertGreaterEqual(dec.batches, 4)
        self.assertEqual(c.decode_message(out[0]), text)

    def test_inverted_polarity_and_bit_errors(self):
        text = "Hello World, a message with a few bit errors on the way"
        bits = c.build_transmission(654321, 3, c.text_to_bits(text))
        out, dec = decode_bits(bits, flip=1, error_rate=0.002, seed=3)
        self.assertEqual(len(out), 1)
        self.assertEqual(c.decode_message(out[0]), text)
        self.assertEqual(out[0]["polarity"], 1)
        self.assertGreater(out[0]["corrected"], 0)

    def test_silence_and_noise_do_not_create_calls(self):
        out, _ = decode_bits([0] * 4000)
        self.assertEqual(out, [])
        rng = random.Random(5)
        out, _ = decode_bits([rng.getrandbits(1) for _ in range(20000)])
        self.assertEqual(out, [])

    def test_argument_validation(self):
        with self.assertRaises(ValueError):
            c.build_transmission(c.RIC_MAX + 1, 0)
        with self.assertRaises(ValueError):
            c.build_transmission(1, 4)
        with self.assertRaises(ValueError):
            c.build_transmission(0, 0)                # all-zero address codeword = silence


class AudioEncoderTests(unittest.TestCase):
    def test_nrz_polarity_deviation_and_duration(self):
        for baud in c.BAUD_RATES:
            audio, dur = pocsag.encode_message(1234567, 3, "alpha", "DA2JH", baud)
            bits = len(pocsag.message_bits(1234567, 3, "alpha", "DA2JH"))
            self.assertAlmostEqual(dur, bits / baud, delta=1.0 / 48000 * 2)
            self.assertLessEqual(float(np.abs(audio).max()), 1.0 + 1e-6)
            self.assertGreater(float(np.abs(audio).max()), 0.98)
            # decode the shaped audio back to bits: logic 1 = negative
            sps = 48000 / baud
            sliced = [1 if audio[int((k + 0.5) * sps)] < 0 else 0 for k in range(int(len(audio) / sps))]
            out, _ = decode_bits(sliced)
            self.assertEqual(c.decode_message(out[0]), "DA2JH")

    def test_rejects_unknown_baud_and_kind(self):
        with self.assertRaises(ValueError):
            pocsag.encode_message(1, 0, "alpha", "x", 300)
        with self.assertRaises(ValueError):
            pocsag.encode_message(1, 0, "voice", "x", 1200)


if __name__ == "__main__":
    unittest.main()
