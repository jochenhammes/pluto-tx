"""APRS demodulator and station state on synthetic audio (no GNU Radio)."""
import json
import unittest

import numpy as np

from pluto_advanced_rx import aprs_state
from pluto_advanced_rx.aprs_demod import AprsFrameDetector
from pluto_advanced_rx.aprs_state import AprsState
from pluto_tx import aprs, aprs_codec as c
from tests import aprs_measure as m

RATE = 12_500


def frame(source="DA2JH-9", info=b"!5330.00N/00900.00E>TEST", path=("WIDE1-1",)):
    return c.build_frame(source, "APRS", list(path), info)


def detect(audio, rate=RATE, chunk=4096):
    got = []
    det = AprsFrameDetector(rate, got.append)
    for i in range(0, len(audio), chunk):
        det.feed(audio[i:i + chunk])
    return got, det


def tone(fr, rate=RATE, **kw):
    pad = np.zeros(int(0.1 * rate), np.float32)
    return np.concatenate([pad, aprs.encode_frame(fr, rate, **kw), pad])


class ModemTests(unittest.TestCase):
    def test_round_trip(self):
        got, det = detect(tone(frame()))
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]["source"], "DA2JH-9")
        self.assertEqual(got[0]["info"], b"!5330.00N/00900.00E>TEST")
        self.assertEqual(det.frames_ok, 1)

    def test_rates(self):
        for rate in (12_000, 12_500, 12_800, 13_200, 16_000, 22_050, 44_100, 48_000):
            with self.subTest(rate=rate):
                self.assertEqual(len(detect(tone(frame(), rate), rate)[0]), 1)

    def test_all_byte_values_and_stuffing(self):
        info = bytes(range(1, 256))
        self.assertEqual(detect(tone(frame(info=info)))[0][0]["info"], info)

    def test_block_boundaries_lose_nothing(self):
        audio = np.concatenate([tone(frame(info=f"!packet {i}".encode())) for i in range(10)])
        for chunk in (7, 100, 5000, 100000):
            with self.subTest(chunk=chunk):
                self.assertEqual(len(detect(audio, chunk=chunk)[0]), 10)

    def test_a_frame_found_by_several_slicers_counts_once(self):
        self.assertEqual(len(detect(tone(frame()))[0]), 1)

    def test_tilt_both_ways(self):
        for db in (-9, -6, 6, 12):  # at -12 dB neither this nor multimon-ng decodes: the correlators leak Mark into Space
            with self.subTest(db=db):
                x = m.tilt(tone(frame(), m.RATE), db)
                self.assertEqual(len(detect(x, m.RATE)[0]), 1)

    def test_clock_error(self):
        for error in (-0.02, 0.02):
            with self.subTest(error=error):
                self.assertEqual(len(detect(tone(frame(), RATE * (1 + error)))[0]), 1)

    def test_short_preamble(self):
        self.assertEqual(len(detect(tone(frame(), preamble_s=0.02))[0]), 1)

    def test_noise_gives_nothing(self):
        got, _ = detect(np.random.default_rng(1).standard_normal(RATE * 30).astype(np.float32))
        self.assertEqual(got, [])

    def test_sensitivity_and_multimon_comparison_floor(self):
        x, sent = m.scene(30, np.random.default_rng(1), snr_db=4)
        self.assertGreaterEqual(m.ours(x, sent)[0], 29)


class AudioTests(unittest.TestCase):
    def test_airtime_matches_audio(self):
        fr = frame()
        self.assertAlmostEqual(len(aprs.encode_frame(fr, 48000)) / 48000, aprs.airtime_s(fr), delta=0.002)

    def test_phase_is_continuous(self):
        x = aprs.encode_frame(frame(), 48000)
        self.assertLess(np.abs(np.diff(x)).max(), 2 * np.pi * 2200 / 48000 * aprs.AMPLITUDE * 1.01)


class StateTests(unittest.TestCase):
    def on(self, state, info, source="DA2JH-9", now=100.0):
        return state.on_frame(c.parse_frame(c.build_frame(source, "APRS", [], info)), now=now)

    def test_station_position_and_trail(self):
        state = AprsState()
        self.on(state, b"!5330.00N/00900.00E>088/036/A=001234 hi", now=100.0)
        self.on(state, b"!5331.00N/00900.00E>088/036", now=130.0)
        s = state.get_stations(now=130.0)[0]
        self.assertEqual(s["label"], "DA2JH-9")
        self.assertAlmostEqual(s["lat"], 53 + 31 / 60, places=4)
        self.assertEqual(s["packets"], 2)
        self.assertEqual(len(s["trail"]), 2)
        self.assertEqual(s["course_deg"], 88)
        json.dumps(s)

    def test_status_keeps_the_position(self):
        state = AprsState()
        self.on(state, b"!5330.00N/00900.00E>x")
        self.on(state, b">gone fishing", now=101.0)
        s = state.get_stations(now=101.0)[0]
        self.assertAlmostEqual(s["lat"], 53.5, places=3)
        self.assertEqual(s["comment"], "gone fishing")

    def test_object_is_its_own_entry(self):
        state = AprsState()
        self.on(state, b";LEADER   *092345z4903.50N/07201.75W>x")
        s = state.get_stations(now=100.0)[0]
        self.assertEqual((s["label"], s["reported_by"], s["alive"]), ("LEADER", "DA2JH-9", True))

    def test_station_without_position_is_listed(self):
        state = AprsState()
        self.on(state, b">status only")
        self.assertIsNone(state.get_stations(now=100.0)[0]["lat"])

    def test_messages_are_collected(self):
        state = AprsState()
        self.on(state, c.message_info("DL1ABC", "Hallo", "7"))
        self.on(state, c.ack_info("DA2JH-9", "7"), source="DL1ABC", now=101.0)
        messages = state.get_messages()
        self.assertEqual([m_["kind"] for m_ in messages], ["message", "ack"])
        self.assertEqual(messages[0]["message"], "Hallo")

    def test_take_changed_and_clear(self):
        state = AprsState()
        self.on(state, b"!5330.00N/00900.00E>x")
        changed = state.take_changed(now=100.0)
        self.assertEqual([s["label"] for s in changed], ["DA2JH-9"])
        self.assertNotIn("trail", changed[0])
        self.assertEqual(state.take_changed(now=100.0), [])
        state.clear()
        self.assertEqual(state.counts(), (0, 0))

    def test_limits_and_expiry(self):
        state = AprsState()
        for i in range(aprs_state.MAX_ROWS + 5):
            self.on(state, b">x", source=f"A{i % 900:03d}", now=float(i))
        self.assertEqual(state.counts()[0], aprs_state.MAX_ROWS)
        self.assertEqual(state.get_stations(now=1e9), [])


if __name__ == "__main__":
    unittest.main()
