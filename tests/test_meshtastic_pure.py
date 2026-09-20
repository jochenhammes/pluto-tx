"""Hardware-free, GNU-Radio-free tests for the Meshtastic building blocks.
Run: python3 -m unittest discover tests"""
import unittest

from pluto_tx import config, lora_airtime, meshtastic_codec as mc


class AirtimeTests(unittest.TestCase):
    def test_221_byte_longfast_frame_matches_measured_gr_lora_sdr_output(self):
        # 1845.2 ms was counted from the samples modulate emitted with the
        # (then default) 8-symbol preamble; Meshtastic's real 16 adds 8 symbols.
        t8 = lora_airtime.lora_airtime_s(221, 11, 250_000, 1, 8)
        self.assertAlmostEqual(t8, 1.8452, places=3)
        t16 = lora_airtime.lora_airtime_s(221, 11, 250_000, 1, config.MESHTASTIC_PREAMBLE_LEN)
        self.assertAlmostEqual(t16 - t8, 8 * 2 ** 11 / 250_000, places=6)

    def test_cr_index(self):
        self.assertEqual(lora_airtime.cr_index("4/5"), 1)
        self.assertEqual(lora_airtime.cr_index("4/8"), 4)
        self.assertEqual(lora_airtime.cr_index("8"), 4)  # MeshCore placeholder notation
        with self.assertRaises(ValueError):
            lora_airtime.cr_index("4/9")


class DutyCycleTests(unittest.TestCase):
    def test_budget_and_window(self):
        d = lora_airtime.DutyCycleLimiter(0.10)  # 360 s / h
        self.assertEqual(d.budget_s(), 360.0)
        ok, _ = d.check(300.0, now=0.0)
        self.assertTrue(ok)
        d.record(300.0, now=0.0)
        ok, wait = d.check(100.0, now=10.0)
        self.assertFalse(ok)
        self.assertAlmostEqual(wait, 3590.0)  # until the 300 s entry leaves the 1 h window
        ok, _ = d.check(100.0, now=3601.0)
        self.assertTrue(ok)

    def test_frame_larger_than_budget_never_fits(self):
        d = lora_airtime.DutyCycleLimiter(0.001)
        ok, wait = d.check(10.0, now=0.0)
        self.assertFalse(ok)
        self.assertEqual(wait, float("inf"))

    def test_no_limit(self):
        d = lora_airtime.DutyCycleLimiter(None)
        self.assertEqual(d.check(1e9, now=0.0), (True, 0.0))


class PresetTests(unittest.TestCase):
    def test_preset_table_matches_firmware(self):
        # (SF, BW, CR) from firmware MeshRadio.h modemPresetToParams()
        expected = {"LongFast": (11, 250e3, "4/5"), "LongModerate": (11, 125e3, "4/8"), "LongSlow": (12, 125e3, "4/8"),
                    "MediumFast": (9, 250e3, "4/5"), "MediumSlow": (10, 250e3, "4/5"), "ShortFast": (7, 250e3, "4/5"),
                    "ShortSlow": (8, 250e3, "4/5"), "ShortTurbo": (7, 500e3, "4/5"), "LongTurbo": (11, 500e3, "4/8")}
        for name, phy in expected.items():
            p = config.MESHTASTIC_PRESETS[config.meshtastic_preset_index(name, "EU433")]
            self.assertEqual((p.spreading_factor, p.bandwidth_hz, p.coding_rate), phy)
        self.assertEqual(len(config.MESHTASTIC_PRESETS), 16)  # no 500 kHz presets on the 250 kHz EU868 slice
        with self.assertRaises(ValueError):
            config.meshtastic_preset_index("ShortTurbo", "EU868")

    def test_carrier_frequencies_follow_the_firmware_slot_formula(self):
        f = lambda n, r: config.MESHTASTIC_PRESETS[config.meshtastic_preset_index(n, r)].frequency_hz
        self.assertEqual(f("LongFast", "EU868"), 869_525_000.0)  # the one real-hardware-verified carrier
        self.assertEqual(f("LongFast", "EU433"), 433_875_000.0)  # djb2("LongFast") % 4 == 3
        for p in config.MESHTASTIC_PRESETS:
            lo, hi = ((433e6, 434e6) if p.ham_mode_required else (869.4e6, 869.65e6))
            self.assertLessEqual(lo + p.bandwidth_hz / 2, p.frequency_hz)
            self.assertLessEqual(p.frequency_hz + p.bandwidth_hz / 2, hi)

    def test_default_channel_hash_byte_per_preset(self):
        self.assertEqual(mc.channel_hash("LongFast", mc.DEFAULT_CHANNEL_PSK), 0x08)  # measured
        names = {p.default_channel_name for p in config.MESHTASTIC_PRESETS}
        self.assertIn("LongMod", names)  # firmware display name of LongModerate

    def test_sync_symbols(self):
        self.assertEqual(config.meshtastic_sync_symbols(11), config.MESHTASTIC_SYNC_SYMBOLS)  # measured (16, 2008)
        self.assertEqual(config.meshtastic_sync_symbols(7), (16, 88))  # coincides with the nominal 0x2B symbols
        for sf in range(7, 13):
            self.assertLess(config.meshtastic_sync_symbols(sf)[1], 2 ** sf)

    def test_only_longfast_is_marked_verified(self):
        for p in config.MESHTASTIC_PRESETS:
            self.assertEqual(p.phy_verified, p.default_channel_name == "LongFast")

    def test_meshcore_presets_are_separate_and_carry_their_own_framing(self):
        self.assertTrue(config.MESHCORE_PRESETS)
        self.assertFalse(set(config.MESHCORE_PRESETS) & set(config.MESHTASTIC_PRESETS))
        narrow = config.MESHCORE_PRESETS[0]
        self.assertEqual((narrow.frequency_hz, narrow.spreading_factor, narrow.bandwidth_hz, narrow.coding_rate),
                         (869_618_000.0, 8, 62_500.0, "8"))
        self.assertEqual((narrow.preamble_len, narrow.sync_word, narrow.phy_verified), (32, 0x12, True))
        for p in config.MESHTASTIC_PRESETS:                    # Meshtastic keeps its measured framing
            self.assertEqual((p.preamble_len, p.sync_word), (16, None))

    def test_868_presets_have_duty_cycle_and_no_ham_mode(self):
        for p in config.LORA_PRESETS:
            if 869e6 < p.frequency_hz < 870e6:
                self.assertEqual(p.duty_cycle_limit, 0.10)
                self.assertFalse(p.ham_mode_required)

    def test_433_preset_is_inside_the_70cm_band(self):
        p = config.MESHTASTIC_PRESETS[0]
        self.assertEqual(config.in_amateur_band(p.frequency_hz), "70cm")
        self.assertTrue(p.ham_mode_required)


class CodecTests(unittest.TestCase):
    def test_parse_psk(self):
        self.assertEqual(mc.parse_psk("AQ=="), mc.DEFAULT_CHANNEL_PSK)
        self.assertEqual(mc.parse_psk(""), b"")
        self.assertEqual(len(mc.parse_psk("AAAAAAAAAAAAAAAAAAAAAA==")), 16)
        for bad in ("zz", "AQID"):
            with self.assertRaises(ValueError):
                mc.parse_psk(bad)

    def test_node_ids_are_valid(self):
        for _ in range(200):
            n = mc.random_node_id()
            self.assertTrue(4 <= n < mc.BROADCAST_ADDR)

    def test_text_roundtrip_default_channel(self):
        raw = mc.build_text_packet("Hallo Welt", 0x1234ABCD)
        s = mc.summarize_packet(raw)
        self.assertEqual((s["kind"], s["text"], s["from"], s["channel_match"]), ("text", "Hallo Welt", 0x1234ABCD, True))
        self.assertEqual(s["channel_hash"], 0x08)  # measured from a real Heltec V3

    def test_wrong_channel_is_flagged(self):
        raw = mc.build_text_packet("x", 1234567, channel_name="Other", psk=bytes(16))
        s = mc.summarize_packet(raw)  # default channel
        self.assertFalse(s["channel_match"])

    def test_unencrypted_roundtrip(self):
        raw = mc.build_text_packet("cq", 5555, psk=b"")
        s = mc.summarize_packet(raw, psk=b"")
        self.assertEqual(s["text"], "cq")

    def test_garbage_never_raises(self):
        for raw in (b"", b"\x00" * 5, bytes(range(40))):
            self.assertIn("kind", mc.summarize_packet(raw))

    def test_real_packet_bytes_reproduced(self):
        # encode_packet() == a real captured Heltec V3 packet, byte for byte
        # (see the module docstrings); here: header layout invariants only.
        raw = mc.build_text_packet("A", 0x43B59FCC, packet_id=0x11223344, hop_limit=3)
        self.assertEqual(raw[:4], b"\xff\xff\xff\xff")
        self.assertEqual(raw[4:8], (0x43B59FCC).to_bytes(4, "little"))
        self.assertEqual(raw[8:12], (0x11223344).to_bytes(4, "little"))
        self.assertEqual(raw[12], 0x03 | (3 << 5))
        self.assertEqual(raw[15], 0xCC)  # relay node = low byte of the sender


if __name__ == "__main__":
    unittest.main()
