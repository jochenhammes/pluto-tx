"""AX.25 and the APRS information field against known values and vectors from aprslib (no GNU Radio)."""
import json
import os
import random
import unittest

from pluto_tx import aprs_codec as c

VECTORS = os.path.join(os.path.dirname(__file__), "data", "aprs", "vectors.jsonl")


class FrameTests(unittest.TestCase):
    def test_fcs_check_value(self):
        self.assertEqual(c.fcs16(b"123456789"), 0x906E)  # the published CRC-16/X.25 check value

    def test_round_trip(self):
        frame = c.build_frame("DA2JH-9", "APRS", ["WIDE1-1", "WIDE2-1"], b"!5330.00N/00900.00E>test")
        parsed = c.parse_frame(frame)
        self.assertEqual(parsed["source"], "DA2JH-9")
        self.assertEqual(parsed["destination"], "APRS")
        self.assertEqual(parsed["path"], ["WIDE1-1", "WIDE2-1"])
        self.assertEqual(parsed["info"], b"!5330.00N/00900.00E>test")

    def test_known_address_bytes(self):
        frame = c.build_frame("N0CALL", "APRS", [], b"x")
        self.assertEqual(frame[:7], bytes((0x82, 0xA0, 0xA4, 0xA6, 0x40, 0x40, 0x60)))
        self.assertEqual(frame[7:14], bytes((0x9C, 0x60, 0x86, 0x82, 0x98, 0x98, 0x61)))
        self.assertEqual(frame[14:16], bytes((0x03, 0xF0)))

    def test_heard_via_digipeater(self):
        frame = bytearray(c.build_frame("DA2JH", "APRS", ["DB0XY-10", "WIDE2-1"], b"x"))
        frame[20] |= 0x80  # has-been-repeated bit of the first digipeater
        body = bytes(frame[:-2])
        parsed = c.parse_frame(body + c.fcs16(body).to_bytes(2, "little"))
        self.assertEqual(parsed["heard_via"], "DB0XY-10")
        self.assertEqual(parsed["path"], ["DB0XY-10*", "WIDE2-1"])

    def test_every_single_bit_error_is_refused(self):
        frame = c.build_frame("DA2JH-9", "APRS", ["WIDE1-1"], b"!5330.00N/00900.00E>test")
        for bit in range(len(frame) * 8):
            bad = bytearray(frame)
            bad[bit // 8] ^= 1 << (bit % 8)
            self.assertIsNone(c.parse_frame(bytes(bad)), bit)

    def test_invalid_input(self):
        for call in ("", "TOOLONG7", "da2jh", "A-16", "A B"):
            with self.subTest(call=call):
                with self.assertRaises(ValueError):
                    c.build_frame(call, "APRS", [], b"x")
        with self.assertRaises(ValueError):
            c.build_frame("DA2JH", "APRS", ["WIDE1-1"] * 9, b"x")

    def test_stuffing_never_has_six_ones_in_data(self):
        frame = c.build_frame("DA2JH", "APRS", [], bytes((0xFF,)) * 20)
        bits = c.hdlc_bits(frame, preamble_flags=0, tail_flags=0)[8:]
        run = 0
        for b in bits:
            run = run + 1 if b else 0
            self.assertLessEqual(run, 5)


class AprslibVectorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(VECTORS, encoding="utf-8") as f:
            cls.vectors = [json.loads(line) for line in f]

    def check(self, formats, minimum):
        checked = 0
        for v in self.vectors:
            expected = v["aprslib"]
            if expected.get("format") not in formats:
                continue
            checked += 1
            info = c.parse_info(v["info"].encode("latin-1"), v["destination"])
            with self.subTest(info=v["info"], destination=v["destination"]):
                self.assertAlmostEqual(info["lat"], expected["latitude"], places=3)
                self.assertAlmostEqual(info["lon"], expected["longitude"], places=3)
                self.assertEqual(info["symbol"], expected["symbol_table"] + expected["symbol"])
                if info["kind"] == "weather":  # here ccc/sss is the wind, which aprslib calls course and speed
                    self.assertEqual(info.get("wind_dir_deg"), expected["course"])
                    continue
                if expected.get("course"):
                    self.assertEqual(info.get("course_deg"), expected["course"] % 360)
                if expected.get("speed") is not None and expected.get("course"):
                    self.assertAlmostEqual(info.get("speed_kt"), expected["speed"] / 1.852, delta=1.1)
                if "altitude" in expected:
                    self.assertAlmostEqual(info.get("alt_ft"), expected["altitude"] / 0.3048, delta=4)
        self.assertGreater(checked, minimum)

    def test_uncompressed(self):
        self.check({"uncompressed"}, 100)

    def test_compressed(self):
        self.check({"compressed"}, 60)

    def test_mic_e(self):
        self.check({"mic-e"}, 100)

    def test_messages(self):
        checked = 0
        for v in self.vectors:
            expected = v["aprslib"]
            if expected.get("format") != "message":
                continue
            checked += 1
            info = c.parse_info(v["info"].encode())
            self.assertEqual(info["addressee"], expected["addresse"])
            if expected.get("response") == "ack":
                self.assertEqual(info["kind"], "ack")
                self.assertEqual(info["message_id"], expected["msgNo"])
            else:
                self.assertEqual(info["kind"], "message")
                self.assertEqual(info["message"], expected["message_text"])
                self.assertEqual(info["message_id"], expected.get("msgNo"))
        self.assertGreaterEqual(checked, 4)


class SpecificationExampleTests(unittest.TestCase):
    def test_position_with_timestamp(self):
        info = c.parse_info(b"/092345z4903.50N/07201.75W>Test1234")
        self.assertEqual(info["kind"], "position")
        self.assertAlmostEqual(info["lat"], 49.0583, places=3)
        self.assertAlmostEqual(info["lon"], -72.0292, places=3)
        self.assertEqual(info["comment"], "Test1234")
        self.assertEqual(info["timestamp"], "092345z")

    def test_course_speed_altitude(self):
        info = c.parse_info(b"!4903.50N/07201.75W>088/036/A=001234 hi")
        self.assertEqual((info["course_deg"], info["speed_kt"], info["alt_ft"]), (88, 36.0, 1234))
        self.assertEqual(info["comment"], "hi")

    def test_ambiguous_position_takes_the_middle(self):
        info = c.parse_info(b"!49  .  N/072  .  W>x")
        self.assertAlmostEqual(info["lat"], 49 + 55.55 / 60, places=3)

    def test_weather(self):
        info = c.parse_info(b"=4903.50N/07201.75W_220/004g005t077r000p000P000h50b09900wRSW")
        self.assertEqual(info["kind"], "weather")
        self.assertEqual(info["temperature_c"], 25.0)
        self.assertEqual(info["wind_dir_deg"], 220)
        self.assertEqual(info["pressure_hpa"], 990.0)
        self.assertEqual(info["humidity_pct"], 50.0)

    def test_object_and_item(self):
        info = c.parse_info(b";LEADER   *092345z4903.50N/07201.75W>088/036")
        self.assertEqual((info["kind"], info["name"], info["alive"]), ("object", "LEADER", True))
        self.assertAlmostEqual(info["lat"], 49.0583, places=3)
        info = c.parse_info(b")AID #2!4903.50N/07201.75WA")
        self.assertEqual((info["kind"], info["name"]), ("item", "AID #2"))

    def test_status_bulletin_telemetry(self):
        self.assertEqual(c.parse_info(b">Net Control")["comment"], "Net Control")
        self.assertEqual(c.parse_info(b":BLN3     :Snow")["kind"], "bulletin")
        self.assertEqual(c.parse_info(b"T#005,199,000,255,073,123,01101001")["kind"], "telemetry")

    def test_third_party(self):
        info = c.parse_info(b"}DL1ABC>APRS,TCPIP,DB0XY*:!4903.50N/07201.75W>inner")
        self.assertEqual((info["kind"], info["third_party_source"]), ("position", "DL1ABC"))

    def test_unknown_keeps_the_text(self):
        info = c.parse_info(b"Hello unknown")
        self.assertEqual((info["kind"], info["text"]), ("other", "Hello unknown"))


class RobustnessTests(unittest.TestCase):
    def test_random_bytes_never_raise(self):
        rng = random.Random(1)
        prefixes = [b"!", b"=", b"/", b"@", b"`", b"'", b";", b")", b":", b">", b"_", b"T#", b"}", b"$", b""]
        for _ in range(20000):
            info = rng.choice(prefixes) + bytes(rng.getrandbits(8) for _ in range(rng.randint(0, 60)))
            result = c.parse_info(info, rng.choice(["APRS", "S32U6T", "", "ABCDEF"]))
            self.assertIn("kind", result)
            json.dumps(result)

    def test_random_frames_never_raise(self):
        rng = random.Random(2)
        for _ in range(5000):
            body = bytes(rng.getrandbits(8) for _ in range(rng.randint(0, 120)))
            self.assertIn(c.parse_frame(body + c.fcs16(body).to_bytes(2, "little")) is None, (True, False))


class BuildingTests(unittest.TestCase):
    def test_beacon_reads_back(self):
        info = c.parse_info(c.beacon_info(53.5, 9.0, "/>", "TEST"))
        self.assertEqual(info["kind"], "position")
        self.assertAlmostEqual(info["lat"], 53.5, places=3)
        self.assertAlmostEqual(info["lon"], 9.0, places=3)
        self.assertEqual(info["symbol"], "/>")
        self.assertEqual(info["comment"], "TEST")

    def test_south_west_and_rounding_at_60_minutes(self):
        info = c.parse_info(c.beacon_info(-33.99999, -70.99999, "/-"))
        self.assertAlmostEqual(info["lat"], -34.0, places=3)
        self.assertAlmostEqual(info["lon"], -71.0, places=3)

    def test_message_and_ack_read_back(self):
        info = c.parse_info(c.message_info("DL1ABC", "Hallo", "7"))
        self.assertEqual((info["kind"], info["addressee"], info["message"], info["message_id"]), ("message", "DL1ABC", "Hallo", "7"))
        info = c.parse_info(c.ack_info("DL1ABC", "7"))
        self.assertEqual((info["kind"], info["message_id"]), ("ack", "7"))

    def test_refusals(self):
        for call, kw in ((lambda: c.message_info("bad call", "x"), None), (lambda: c.message_info("DL1ABC", ""), None),
                         (lambda: c.message_info("DL1ABC", "x" * 68), None), (lambda: c.message_info("DL1ABC", "a{b"), None),
                         (lambda: c.message_info("DL1ABC", "Grüße"), None), (lambda: c.beacon_info(91, 0, "/>"), None),
                         (lambda: c.beacon_info(0, 0, ">"), None), (lambda: c.beacon_info(0, 0, "/>", "x" * 44), None)):
            with self.assertRaises(ValueError):
                call()


if __name__ == "__main__":
    unittest.main()
