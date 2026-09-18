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
    def test_meshtastic_presets_share_phy(self):
        # the TX/RX chains are built once from MESHTASTIC_PRESETS[0]; presets may only differ in frequency
        first = config.MESHTASTIC_PRESETS[0]
        for p in config.MESHTASTIC_PRESETS:
            self.assertEqual((p.spreading_factor, p.bandwidth_hz, p.coding_rate),
                             (first.spreading_factor, first.bandwidth_hz, first.coding_rate))

    def test_meshcore_is_placeholder_only(self):
        self.assertTrue(config.MESHCORE_PRESETS)
        self.assertFalse(set(config.MESHCORE_PRESETS) & set(config.MESHTASTIC_PRESETS))

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
