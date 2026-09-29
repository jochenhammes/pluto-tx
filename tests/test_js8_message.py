"""J1: JS8 message layer (pluto_tx/js8_message.py), bit-exact against the JS8Call reference vectors
(tests/data/js8/vectors.jsonl, made by tools/js8ref from the unmodified JS8Call v2.5.2 sources)."""
import json
import os
import unittest

from pluto_tx import js8, js8_message as M, js8_phy as P

DATA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "js8")
HAVE_JSC = M.jsc_available()


def load_vectors():
    with open(os.path.join(DATA, "vectors.jsonl")) as f:
        return [json.loads(line) for line in f]


def uses_data_frames(case):
    return any(fr["frame_type"] == M.FRAME_DATA for fr in case["frames"])


class ReferenceVectorTests(unittest.TestCase):
    def check_case(self, case):
        frames, info = M.build_message_frames(case["mycall"], case["mygrid"], case["selected"], case["text"],
                                              case["force_identify"], case["force_data"], case["submode"])
        builder = [b for _, b in frames]
        frames = M.transmit_flags(frames)
        self.assertEqual(frames, [(fr["frame"], fr["bits"]) for fr in case["frames"]], case["case"])
        self.assertEqual(builder, [fr["builder_bits"] for fr in case["frames"]], case["case"])
        self.assertEqual((info["dir_to"], info["dir_cmd"], info["dir_num"]),
                         (case["dir_to"], case["dir_cmd"], case["dir_num"]), case["case"])
        for fr in case["frames"]:
            d = M.decode_frame(fr["frame"], fr["bits"], case["submode"])
            self.assertEqual(d["message"], fr["message"], (case["case"], fr["frame"]))
            self.assertEqual(d["frame_type"], fr["frame_type"], (case["case"], fr["frame"]))

    def test_frames_without_data(self):
        """Heartbeat/CQ, compound and directed frames need no JSC word lists."""
        cases = [c for c in load_vectors() if not uses_data_frames(c)]
        self.assertGreater(len(cases), 60)
        for case in cases:
            self.check_case(case)

    @unittest.skipUnless(HAVE_JSC, "JSC word lists missing (js8call/jsc.json, install-js8.sh)")
    def test_all_reference_vectors(self):
        cases = load_vectors()
        self.assertEqual(len(cases), 176)
        for case in cases:
            self.check_case(case)


class FieldTests(unittest.TestCase):
    def test_callsign_roundtrip(self):
        for call in ("DA2JH", "DL1ABC", "K1A", "9A1AA", "DA2JH/P", "3DA0XY", "3XY1Z"):
            packed, portable = M.pack_callsign(call)
            self.assertNotEqual(packed, 0, call)
            self.assertEqual(M.unpack_callsign(packed, portable), call)
        self.assertEqual(M.pack_callsign("@ALLCALL")[0], M.NBASECALL + 2)
        self.assertEqual(M.pack_callsign("TOOLONGCALL")[0], 0)

    def test_compound_callsign_roundtrip(self):
        for call in ("DA2JH/MM", "EA8/DA2JH", "DL1ABC/QRP", "@ALLCALL"):
            self.assertEqual(M.unpack_alphanumeric50(M.pack_alphanumeric50(call)), call)
        self.assertTrue(M.is_compound_callsign("DA2JH/MM"))
        self.assertFalse(M.is_compound_callsign("DA2JH/P"))
        self.assertFalse(M.is_compound_callsign("DA2JH"))

    def test_grid_roundtrip(self):
        for grid in ("JO31", "AA00", "RR99", "IL18", "FN42"):
            self.assertEqual(M.unpack_grid(M.pack_grid(grid)), grid)
        self.assertEqual(M.pack_grid(""), M.NMAXGRID)

    def test_snr_format_and_checksums(self):
        self.assertEqual(M.format_snr(-7), "-07")
        self.assertEqual(M.format_snr(5), "+05")
        self.assertEqual(M.format_snr(0), "+00")
        self.assertEqual(M.format_snr(99), "")
        self.assertEqual(M.checksum16("TEST MESSAGE"), "TWT")   # the reference MSG vector
        self.assertTrue(M.checksum16_valid("TWT", "TEST MESSAGE"))
        self.assertEqual(len(M.checksum32("HELLO")), 6)

    def test_huffman_roundtrip(self):
        text = "HELLO WORLD 73?"
        bits = [b for _, code in M.huff_encode(text) for b in code]
        self.assertEqual(M.huff_decode(bits), text)


class ComposeTests(unittest.TestCase):
    def test_compose_matches_js8call_gui_texts(self):
        self.assertEqual(M.compose("cq", "da2jh", "jo31ab"), "CQ CQ CQ JO31")
        self.assertEqual(M.compose("hb", "DA2JH", "JO31"), "DA2JH: HEARTBEAT JO31")
        self.assertEqual(M.compose("snr_query", "DA2JH", to="dl1abc"), "DL1ABC SNR?")
        self.assertEqual(M.compose("snr_reply", "DA2JH", to="DL1ABC", snr_db=-12), "DL1ABC SNR -12")
        self.assertEqual(M.compose("ack", "DA2JH", to="DL1ABC"), "DL1ABC ACK")
        self.assertEqual(M.compose("allcall", "DA2JH", text="hello"), "@ALLCALL HELLO")
        self.assertEqual(M.compose("directed", "DA2JH", to="DL1ABC", text="hi there"), "DL1ABC HI THERE")
        self.assertEqual(M.compose("directed", "DA2JH", text="x"), "")

    def test_normalize_text(self):
        self.assertEqual(M.normalize_text("hello world€\t"), "HELLO WORLD")
        if HAVE_JSC:
            self.assertEqual(M.normalize_text("grüße"), "GRÜSSE")
            self.assertEqual(M.normalize_text("a\nb"), "A\nB")      # "\n" is an extended JSC character

    def test_every_frame_decodes_back(self):
        for sm in (P.NORMAL, P.FAST, P.TURBO, P.SLOW):
            for kind, kw in (("cq", {}), ("hb", {}), ("snr_query", {"to": "DL1ABC"}),
                             ("snr_reply", {"to": "DL1ABC/MM", "snr_db": -3}), ("ack", {"to": "DL1ABC"})):
                text = M.compose(kind, "DA2JH", "JO31", **kw)
                frames = M.build_frames("DA2JH", "JO31", text, sm)
                self.assertTrue(frames)
                for f, b in frames:
                    self.assertEqual(P.decode_tones(P.encode(f, b, sm)), (f, b))

    @unittest.skipUnless(HAVE_JSC, "JSC word lists missing")
    def test_text_roundtrip(self):
        for sm in (P.NORMAL, P.FAST, P.TURBO, P.SLOW):
            for body in ("HELLO WORLD 73", "0123456789 +-./?", "A  B", "QTH JO31 RIG PLUTO SDR ANT DIPOLE"):
                frames = M.build_frames("DA2JH", "JO31", M.compose("allcall", "DA2JH", text=body), sm)
                self.assertEqual(M.frames_text(frames, sm), "DA2JH: @ALLCALL  " + body, (sm, body))
                self.assertEqual(frames[0][1] & P.FLAG_FIRST, P.FLAG_FIRST)
                self.assertEqual(frames[-1][1] & P.FLAG_LAST, P.FLAG_LAST)

    def test_invalid_input_is_rejected_clearly(self):
        with self.assertRaisesRegex(ValueError, "callsign"):
            M.build_frames("NOT A CALL", "JO31", "CQ CQ CQ JO31")
        with self.assertRaisesRegex(ValueError, "cannot encode|JSC"):
            M.build_frames("DA2JH", "JO31", "@ALLCALL hello", P.FAST)        # lowercase: JS8Call hangs here
        with self.assertRaisesRegex(ValueError, "nothing"):
            M.build_frames("DA2JH", "JO31", "")
        self.assertFalse(js8.can_encode("DA2JH", "JO31", "@ALLCALL " + "X" * 400, P.TURBO))


if __name__ == "__main__":
    unittest.main()
