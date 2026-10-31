"""ADS-B messages and positions against the published examples and vectors from pyModeS (no GNU Radio)."""
import json
import os
import unittest

from pluto_advanced_rx import adsb_state
from pluto_advanced_rx.adsb_state import AdsbState
from pluto_tx import adsb_codec as c
from pluto_tx import adsl_codec
from pluto_tx.mode_s_crc import crc24
from tests import adsb_testgen as gen

VECTORS = os.path.join(os.path.dirname(__file__), "data", "adsb", "vectors.jsonl")


def parse(hex_message):
    return c.parse_message(bytes.fromhex(hex_message))


class PublishedExampleTests(unittest.TestCase):
    """From J. Sun, The 1090 Megahertz Riddle."""

    def test_identification(self):
        self.assertEqual(parse("8D4840D6202CC371C32CE0576098")["callsign"], "KLM1023")

    def test_global_position(self):
        even, odd = parse("8D40621D58C382D690C8AC2863A7"), parse("8D40621D58C386435CC412692AD6")
        self.assertFalse(even["cpr_odd"])
        self.assertTrue(odd["cpr_odd"])
        self.assertEqual(even["alt_ft"], 38000)
        lat, lon = c.cpr_global((even["cpr_lat"], even["cpr_lon"]), (odd["cpr_lat"], odd["cpr_lon"]), newest_is_odd=False)
        self.assertAlmostEqual(lat, 52.2572, places=3)
        self.assertAlmostEqual(lon, 3.9194, places=3)

    def test_local_position(self):
        even = parse("8D40621D58C382D690C8AC2863A7")
        lat, lon = c.cpr_local(even["cpr_lat"], even["cpr_lon"], False, (52.258, 3.918))
        self.assertAlmostEqual(lat, 52.2572, places=3)
        self.assertAlmostEqual(lon, 3.9194, places=3)

    def test_ground_speed(self):
        v = parse("8D485020994409940838175B284F")
        self.assertAlmostEqual(v["speed_kt"], 159.2, places=1)
        self.assertAlmostEqual(v["track_deg"], 182.88, places=2)
        self.assertEqual(v["vrate_fpm"], -832)
        self.assertEqual(v["speed_type"], "ground")

    def test_air_speed(self):
        v = parse("8DA05F219B06B6AF189400CBC33F")
        self.assertEqual(v["speed_kt"], 375)
        self.assertAlmostEqual(v["heading_deg"], 243.98, places=2)
        self.assertEqual(v["vrate_fpm"], -2304)
        self.assertEqual(v["speed_type"], "true air")

    def test_surface(self):
        s = parse("8C4841753A9A153237AEF0F275BE")
        self.assertEqual(s["speed_kt"], 17)
        self.assertAlmostEqual(s["track_deg"], 92.81, places=2)
        self.assertTrue(s["on_ground"])
        lat, lon = c.cpr_local(s["cpr_lat"], s["cpr_lon"], s["cpr_odd"], (51.990, 4.375), surface=True)
        self.assertAlmostEqual(lat, 52.32056, places=4)
        self.assertAlmostEqual(lon, 4.73574, places=4)


class PyModeSVectorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(VECTORS, encoding="utf-8") as f:
            cls.vectors = [json.loads(line) for line in f]

    def test_every_vector_holds_its_crc(self):
        self.assertGreater(len(self.vectors), 800)
        for v in self.vectors:
            self.assertTrue(c.check_message(bytes.fromhex(v["message"])))

    def test_callsigns(self):
        for v in (v for v in self.vectors if "callsign" in v):
            self.assertEqual(parse(v["message"])["callsign"].replace(" ", ""), v["callsign"].replace(" ", ""), v["message"])

    def test_altitude(self):
        for v in (v for v in self.vectors if "odd" in v):
            info = parse(v["message"])
            self.assertEqual(info["cpr_odd"], bool(v["odd"]))
            q_bit = bytes.fromhex(v["message"])[5] & 1
            if q_bit:
                self.assertEqual(info["alt_ft"], v["alt_ft"], v["message"])
            else:  # Gillham coded: pyModeS decodes it, this receiver deliberately reports it as unknown
                self.assertIsNone(info["alt_ft"], v["message"])

    def test_velocity(self):
        for v in (v for v in self.vectors if "subtype" in v):
            info = parse(v["message"])
            if v["speed"] is not None:
                self.assertAlmostEqual(info["speed_kt"], v["speed"], delta=1.0, msg=v["message"])  # pyModeS truncates
            if v["vrate"] is not None:
                self.assertEqual(info["vrate_fpm"], v["vrate"], v["message"])
            direction = info.get("track_deg", info.get("heading_deg"))
            if v["direction"] is not None:
                self.assertAlmostEqual(direction, v["direction"], places=3, msg=v["message"])

    def test_emergency_and_squawk(self):
        names = {0: "none", 1: "general", 2: "medical", 3: "minimum fuel", 4: "no communications", 5: "unlawful interference",
                 6: "downed aircraft", 7: "reserved"}
        for v in (v for v in self.vectors if "squawk" in v):
            info = parse(v["message"])
            self.assertEqual(info["squawk"], v["squawk"], v["message"])
            self.assertEqual(info["emergency"], names[v["emergency"]], v["message"])

    def test_surface(self):
        for v in (v for v in self.vectors if "surface_position" in v):
            info = parse(v["message"])
            if v["surface_speed"] is not None:
                self.assertAlmostEqual(info["speed_kt"], v["surface_speed"], delta=0.51, msg=v["message"])
            if v["surface_track"] is not None:
                self.assertAlmostEqual(info["track_deg"], v["surface_track"], places=3)
            lat, lon = c.cpr_local(info["cpr_lat"], info["cpr_lon"], info["cpr_odd"], tuple(v["reference"]), surface=True)
            self.assertAlmostEqual(lat, v["surface_position"][0], places=4, msg=v["message"])
            self.assertAlmostEqual(lon, v["surface_position"][1], places=4, msg=v["message"])

    def test_global_positions(self):
        checked = 0
        for v in (v for v in self.vectors if "odd_message" in v):
            even, odd = parse(v["message"]), parse(v["odd_message"])
            position = c.cpr_global((even["cpr_lat"], even["cpr_lon"]), (odd["cpr_lat"], odd["cpr_lon"]), v["newest_odd"])
            if v["position"] is None:
                self.assertIsNone(position)
                continue
            checked += 1
            self.assertAlmostEqual(position[0], v["position"][0], places=4)
            self.assertAlmostEqual(position[1], v["position"][1], places=4)
            self.assertAlmostEqual(position[0], v["true_position"][0], delta=0.002)
            self.assertEqual(even["alt_ft"], v["alt_ft"])
        self.assertGreater(checked, 250)


class CrcTests(unittest.TestCase):
    def test_every_single_bit_error_is_seen(self):
        message = bytes.fromhex("8D40621D58C382D690C8AC2863A7")
        for bit in range(112):
            bad = bytearray(message)
            bad[bit // 8] ^= 0x80 >> bit % 8
            self.assertNotEqual(crc24(bytes(bad)), 0, bit)

    def test_shared_with_adsl(self):
        self.assertIs(adsl_codec.crc24, crc24)

    def test_non_adsb_is_refused(self):
        self.assertIsNone(c.parse_message(bytes.fromhex("5D4840D6202CC371C32CE0576098")))  # DF11
        self.assertIsNone(c.parse_message(gen.encode_message(0x123456, 11, 0, df=18, capability=2)))  # TIS-B
        self.assertIsNotNone(c.parse_message(gen.encode_message(0x123456, 11, 0, df=18, capability=0)))


class StateTests(unittest.TestCase):
    ICAO = 0x3C6444
    LAT, LON = 53.5, 9.5

    def feed(self, state, message, now):
        state.on_message(c.parse_message(message), now=now)

    def located(self, reference=None):
        state = AdsbState(reference)
        self.feed(state, gen.position_message(self.ICAO, self.LAT, self.LON, 35000, 0), 100.0)
        self.feed(state, gen.position_message(self.ICAO, self.LAT, self.LON, 35000, 1), 101.0)
        return state

    def test_one_message_is_not_an_aircraft_yet(self):
        state = AdsbState()
        self.feed(state, gen.callsign_message(self.ICAO, "DLH123"), 100.0)
        self.assertEqual(state.get_aircraft(now=100.0), [])
        self.feed(state, gen.callsign_message(self.ICAO, "DLH123"), 101.0)
        self.assertEqual(state.get_aircraft(now=101.0)[0]["callsign"], "DLH123")

    def test_pair_locates_and_trail_grows(self):
        state = self.located()
        a = state.get_aircraft(now=101.0)[0]
        self.assertAlmostEqual(a["lat"], self.LAT, delta=0.001)
        self.assertAlmostEqual(a["lon"], self.LON, delta=0.001)
        self.assertEqual(a["alt_ft"], 35000)
        self.assertEqual(a["alt_type"], "baro")
        self.assertEqual(len(a["trail"]), 1)
        self.feed(state, gen.position_message(self.ICAO, self.LAT + 0.01, self.LON, 35000, 0), 106.0)
        a = state.get_aircraft(now=106.0)[0]
        self.assertAlmostEqual(a["lat"], self.LAT + 0.01, delta=0.001)
        self.assertEqual(len(a["trail"]), 2)
        json.dumps(a)

    def test_old_partner_gives_no_position(self):
        state = AdsbState()
        self.feed(state, gen.position_message(self.ICAO, self.LAT, self.LON, 35000, 0), 100.0)
        self.feed(state, gen.position_message(self.ICAO, self.LAT, self.LON, 35000, 1), 100.0 + adsb_state.CPR_PAIR_MAX_AGE_S + 1)
        self.assertIsNone(state.get_aircraft(now=120.0)[0]["lat"])

    def test_impossible_jump_drops_the_fix(self):
        state = self.located()
        self.feed(state, gen.position_message(self.ICAO, self.LAT + 2.0, self.LON, 35000, 0), 102.0)
        a = state.get_aircraft(now=102.0)[0]
        self.assertIsNone(a["lat"])
        self.assertEqual(state.counts()[1], 1)

    def test_global_fix_far_outside_range_is_refused(self):
        state = self.located(reference=(-30.0, 100.0))
        self.assertIsNone(state.get_aircraft(now=101.0)[0]["lat"])
        self.assertEqual(state.counts()[1], 1)

    def test_a_lost_field_replaces_the_old_value(self):
        state = self.located()
        self.feed(state, gen.encode_message(self.ICAO, 11, 0), 102.0)  # Altitude code 0: unknown
        self.assertIsNone(state.get_aircraft(now=102.0)[0]["alt_ft"])

    def test_surface_needs_a_reference(self):
        message = bytes.fromhex("8C4841753A9A153237AEF0F275BE")
        without = AdsbState()
        without.on_message(c.parse_message(message), now=1.0)
        without.on_message(c.parse_message(message), now=2.0)
        self.assertIsNone(without.get_aircraft(now=2.0)[0]["lat"])
        with_reference = AdsbState((51.990, 4.375))
        with_reference.on_message(c.parse_message(message), now=1.0)
        with_reference.on_message(c.parse_message(message), now=2.0)
        self.assertAlmostEqual(with_reference.get_aircraft(now=2.0)[0]["lat"], 52.32056, places=3)

    def test_take_changed_returns_each_aircraft_once(self):
        state = self.located()
        self.assertEqual([a["icao"] for a in state.take_changed(now=101.0)], ["3C6444"])
        self.assertEqual(state.take_changed(now=101.0), [])
        self.feed(state, gen.callsign_message(self.ICAO, "DLH123"), 102.0)
        self.assertEqual(len(state.take_changed(now=102.0)), 1)

    def test_silent_aircraft_expire_and_limits_hold(self):
        state = self.located()
        self.assertEqual(state.get_aircraft(now=101.0 + adsb_state.EXPIRE_S + 1), [])
        state = AdsbState()
        for i in range(adsb_state.MAX_AIRCRAFT + 5):
            state.on_message(c.parse_message(gen.callsign_message(i + 1, "A")), now=float(i))
        self.assertLessEqual(len(state._aircraft), adsb_state.MAX_AIRCRAFT)

    def test_clear(self):
        state = self.located()
        state.clear()
        self.assertEqual(state.get_aircraft(now=101.0), [])


if __name__ == "__main__":
    unittest.main()
