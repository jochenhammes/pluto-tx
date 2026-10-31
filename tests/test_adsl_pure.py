"""ADS-L packet layer against the reference vectors and the examples printed in the specification (no GNU Radio)."""
import json
import os
import unittest

from pluto_tx import adsl_codec as c
from tests import adsl_testgen as gen

VECTORS = os.path.join(os.path.dirname(__file__), "data", "adsl", "vectors.jsonl")


def load_vectors():
    with open(VECTORS, encoding="utf-8") as f:
        return [json.loads(line) for line in f]


class ReferenceVectorTests(unittest.TestCase):
    """vectors.jsonl comes from the CC0 reference code (see gen_vectors.cc)."""

    @classmethod
    def setUpClass(cls):
        cls.vectors = load_vectors()

    def test_descramble_gives_the_clear_data(self):
        for v in self.vectors:
            body = bytes.fromhex(v["frame"])[1:]
            self.assertEqual(c.xxtea_descramble(body[1:-3]).hex(), v["clear"])

    def test_every_frame_passes_crc(self):
        for v in self.vectors:
            self.assertEqual(c.crc24(bytes.fromhex(v["frame"])[1:]), 0)

    def test_header_and_integer_fields(self):
        for v in self.vectors:
            info = c.parse_frame(bytes.fromhex(v["frame"]))
            self.assertIsNotNone(info)
            self.assertEqual(info["kind"], "traffic")
            self.assertEqual(info["address_table"], v["table"])
            self.assertEqual(info["address"], v["address"])
            self.assertEqual(info["relayed"], bool(v["relay"]))
            self.assertEqual(info["timestamp_s"], v["timestamp"] * 0.25)
            self.assertEqual(info["flight_state"], c.FLIGHT_STATES[v["flight_state"]])
            self.assertEqual(info["category"], c.CATEGORIES.get(v["category"], "reserved"))
            self.assertEqual(info["emergency"], c.EMERGENCIES[v["emergency"]])

    def test_positions(self):
        for v in self.vectors:
            info = c.parse_frame(bytes.fromhex(v["frame"]))
            self.assertAlmostEqual(info["lat"], v["lat"], delta=2 / 93206)
            self.assertAlmostEqual(info["lon"], v["lon"], delta=2 / 46603)

    def test_exponential_fields_agree_with_reference_within_its_rounding(self):
        """The reference returns the middle of each quantisation step; the specification's formula its lower
        edge. They may differ by half a step, which is at most 2/4/8 raw units at the upper exponents."""
        for v in self.vectors:
            info = c.parse_frame(bytes.fromhex(v["frame"]))
            self.assertLessEqual(abs(info["speed_mps"] * 4 - v["ref_speed_qms"]), 4)
            self.assertLessEqual(abs(info["alt_m"] - v["ref_alt_m"]), 4)
            self.assertLessEqual(abs(info["climb_mps"] * 8 - v["ref_climb_8ms"]), 4)

    def test_reference_example_decodes(self):
        info = c.parse_frame(bytes.fromhex(self.vectors[0]["frame"]))
        self.assertEqual(c.address_label(info), "FANET:123456")
        self.assertEqual(info["timestamp_s"], 4.0)
        self.assertAlmostEqual(info["lat"], 48.12345, places=4)
        self.assertAlmostEqual(info["lon"], 8.12345, places=4)
        self.assertEqual(info["alt_m"], 1234)
        self.assertEqual(info["speed_mps"], 45.0)
        self.assertEqual(info["climb_mps"], -5.5)
        self.assertEqual(info["track_deg"], 180.0)


def traffic_field(speed=0, alt=0, climb=0):
    return bytes(8) + bytes((speed, alt & 0xFF, alt >> 8 | (climb & 3) << 6, climb >> 2)) + bytes(3)


class SpecificationExampleTests(unittest.TestCase):
    """The tables in Subpart G.1 of Issue 2."""

    def test_altitude(self):
        for raw, metres in ((0x0000, -320), (0x0140, 0), (0x0528, 1000), (0x3FFE, 61104)):
            self.assertEqual(c.decode_traffic(traffic_field(alt=raw))["alt_m"], metres, hex(raw))
        self.assertIsNone(c.decode_traffic(traffic_field(alt=0x3FFF))["alt_m"])

    def test_ground_speed(self):
        for raw, mps in ((0x00, 0), (0x01, 0.25), (0x03, 0.75), (0xC4, 120), (0xFE, 236)):
            self.assertEqual(c.decode_traffic(traffic_field(speed=raw))["speed_mps"], mps, hex(raw))
        self.assertIsNone(c.decode_traffic(traffic_field(speed=0xFF))["speed_mps"])

    def test_vertical_rate(self):
        for raw, mps in ((0x000, 0), (0x001, 0.125), (0x101, -0.125), (0x048, 10), (0x148, -10)):
            self.assertEqual(c.decode_traffic(traffic_field(climb=raw))["climb_mps"], mps, hex(raw))
        self.assertIsNone(c.decode_traffic(traffic_field(climb=0x1FF))["climb_mps"])

    def test_limit_vertical_rates(self):
        self.assertEqual(c.decode_traffic(traffic_field(climb=0x0FF))["climb_mps"], 119.0)
        self.assertEqual(c.decode_traffic(traffic_field(climb=0x1FE))["climb_mps"], -118.0)

    def test_no_position(self):
        missing = (0x800000).to_bytes(3, "little")
        info = c.decode_traffic(bytes(2) + missing + missing + bytes(7))
        self.assertIsNone(info["lat"])
        self.assertIsNone(info["lon"])

    def test_track_is_invalid_without_speed(self):
        self.assertIsNone(c.decode_traffic(traffic_field(speed=0))["track_deg"])
        self.assertIsNone(c.decode_traffic(traffic_field(speed=0xFF))["track_deg"])


class RejectionTests(unittest.TestCase):
    def setUp(self):
        self.good = gen.frame(gen.traffic_clear())

    def test_encoder_matches_decoder(self):
        info = c.parse_frame(self.good)
        self.assertEqual(info["address"], 0x123456)
        self.assertEqual(info["category"], "glider")
        self.assertEqual(info["alt_m"], 1234)
        self.assertEqual(info["speed_mps"], 45.0)
        self.assertEqual(info["climb_mps"], -5.5)

    def test_every_single_bit_flip_in_the_body_is_rejected(self):
        for bit in range(8, len(self.good) * 8):
            bad = bytearray(self.good)
            bad[bit // 8] ^= 0x80 >> bit % 8
            self.assertIsNone(c.parse_frame(bytes(bad)), bit)

    def test_truncated_and_oversized_frames(self):
        self.assertIsNone(c.parse_frame(self.good[:-1]))
        self.assertIsNone(c.parse_frame(self.good + b"\0"))
        self.assertIsNone(c.parse_frame(b""))

    def test_reed_solomon_frames_are_not_decoded(self):
        body = bytes((0x80,)) + bytes(20)
        frame = bytes((len(body) + 3,)) + body + c.crc24(body).to_bytes(3, "big")
        self.assertIsNone(c.parse_frame(frame))

    def test_reserved_key_stays_unread(self):
        body = bytes((1 << 5,)) + bytes(20)
        info = c.parse_frame(bytes((len(body) + 3,)) + body + c.crc24(body).to_bytes(3, "big"))
        self.assertEqual(info["kind"], "encrypted")
        self.assertNotIn("address", info)

    def test_scrambling_disabled_with_key_3(self):
        info = c.parse_frame(gen.frame(gen.traffic_clear(), key_index=3))
        self.assertEqual(info["key_index"], 3)
        self.assertEqual(info["address"], 0x123456)

    def test_unknown_payload_type_keeps_address_only(self):
        clear = bytes((99,)) + gen.traffic_clear()[1:]
        info = c.parse_frame(gen.frame(clear))
        self.assertEqual(info["kind"], "unknown")
        self.assertEqual(info["address"], 0x123456)
        self.assertNotIn("alt_m", info)

    def test_future_protocol_version_is_not_interpreted_as_traffic(self):
        info = c.parse_frame(gen.frame(gen.traffic_clear(), version=2))
        self.assertEqual(info["kind"], "traffic")
        self.assertNotIn("alt_m", info)


if __name__ == "__main__":
    unittest.main()
