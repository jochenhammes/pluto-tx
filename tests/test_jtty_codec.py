"""JTTY through wsjt-codec (pluto_tx/jtty_codec.py). The digests pin the audio of the codec at the tag in
packaging/components.lock: a new codec version whose signal differs fails here, on purpose, and needs its
protocol read again (docs/jtty/SPEC.md)."""
import hashlib
import os
import unittest
from unittest import mock

import numpy as np

from pluto_tx import config, jtty_codec

REFERENCE = {  # text -> (frames, sha256 of the int16 audio at 12 kHz, lowest tone 1500 Hz), from v3.3.0-beta5
    "CQ DA2JH CQ": (("CQ DA2JH CQ",), "8cc6409f32e460d6"),
    "DA2JH 599 123": (("DA2JH", "599 123"), "e5bbb8aec6644ff8"),
    "TEST DE DA2JH 1234567890": (("TEST", "DE DA", "2JH 1", "23456", "7890"), "70e086aaaa16ea41"),
}
SAMPLE = os.path.join(os.path.dirname(__file__), "data", "jtty", "260807_134110.wav")


@unittest.skipUnless(jtty_codec.available(), "wsjt-codec not installed (install-jtty.sh)")
class CodecTests(unittest.TestCase):
    def test_audio_matches_the_pinned_codec(self):
        for text, (frames, digest) in REFERENCE.items():
            r = jtty_codec.render(text, rate=jtty_codec.SAMPLE_RATE)
            self.assertEqual(r.frames, frames)
            pcm = (r.audio * 32767.0).round().astype("<i2").tobytes()
            self.assertEqual(hashlib.sha256(pcm).hexdigest()[:16], digest, text)

    def test_duration_follows_the_frames(self):
        r = jtty_codec.render("TEST DE DA2JH 1234567890")
        self.assertEqual(r.rate, config.AUDIO_RATE)
        self.assertAlmostEqual(r.duration_s, 5 * 1.888)

    def test_tone_occupies_the_expected_band(self):
        r = jtty_codec.render("CQ DA2JH CQ", tone_hz=1000.0)
        power = np.abs(np.fft.rfft(r.audio)) ** 2
        freqs = np.fft.rfftfreq(len(r.audio), 1 / r.rate)
        inside = power[(freqs > 950) & (freqs < 1150)].sum()
        self.assertGreater(inside / power.sum(), 0.98)

    def test_rejected_before_any_rf_action(self):
        for text, why in (("", "empty"), ("   ", "empty"), ("x" * 81, "longer"), ("a\nb", "one line"), ("%M", "placeholder")):
            with self.assertRaisesRegex(ValueError, why):
                jtty_codec.render(text)
        for tone in (100.0, 2900.0):
            with self.assertRaisesRegex(ValueError, "audio frequency"):
                jtty_codec.render("TEST", tone_hz=tone)

    def test_longest_text_is_one_message(self):
        r = jtty_codec.render("A" * 80)
        self.assertEqual(r.text, "A" * 80)
        self.assertEqual(len(r.frames), 16)

    def test_substitution_is_reported(self):
        r = jtty_codec.render("hallo äö")
        self.assertTrue(r.substituted)
        self.assertEqual(r.text, "HALLO ##")
        self.assertFalse(jtty_codec.render("HALLO").substituted)

    def test_decodes_the_wsjtx_sample_recording(self):
        texts = jtty_codec.decode_pcm_file(SAMPLE)
        self.assertIn("RAN ALL NIGHT ON BAND NOISE - NO FALSE DECODES!", texts)

    def test_render_and_decode_round_trip(self):
        for text in ("CQ DA2JH CQ", "DA2JH 599 123", "HALLO WELT 73 DE DA2JH"):
            r = jtty_codec.render(text, tone_hz=1500.0, rate=jtty_codec.SAMPLE_RATE)
            gap = np.zeros(jtty_codec.SAMPLE_RATE, dtype="<i2")
            pcm = np.concatenate([gap, (r.audio * 3000.0).astype("<i2"), gap]).tobytes()
            finals = jtty_codec.decode_pcm(pcm)
            self.assertEqual([(m["text"], m["state"]) for m in finals], [(text, "complete")])

    def test_ignores_ld_library_path(self):
        with mock.patch.dict(os.environ, {"LD_LIBRARY_PATH": "/nonexistent"}):
            self.assertNotIn("LD_LIBRARY_PATH", jtty_codec._environment())
            self.assertEqual(jtty_codec.render("TEST").frames, ("TEST",))


class MissingCodecTests(unittest.TestCase):
    def test_says_how_to_get_it(self):
        with mock.patch.object(jtty_codec, "codec_program", return_value=None):
            with self.assertRaisesRegex(ValueError, "install-jtty.sh"):
                jtty_codec.render("TEST")
            with self.assertRaisesRegex(RuntimeError, "install-jtty.sh"):
                jtty_codec.JttyStream(lambda e: None)


if __name__ == "__main__":
    unittest.main()
