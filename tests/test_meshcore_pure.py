"""MeshCore packet layer: framing, adverts (Ed25519), group text (AES-128-ECB + HMAC), identity file.
The two reference vectors are the public test vectors of the meshcore-decoder project (MIT)."""
import os
import random
import stat
import tempfile
import unittest

from pluto_tx import meshcore_codec as mc
from pluto_tx import meshcore_identity

# public vectors (github.com/michaelhart/meshcore-decoder: README advert, tests/grouptext-decryption.test.ts)
ADVERT_HEX = ("11007E7662676F7F0850A8A355BAAFBFC1EB7B4174C340442D7D7161C9474A2C94006CE7CF682E58408DD8FCC51906ECA98EBF94A037886BD"
              "ADE7ECD09FD92B839491DF3809C9454F5286D1D3370AC31A34593D569E9A042A3B41FD331DFFB7E18599CE1E60992A076D50238C5B8F857573"
              "75354522F50756765744D65736820436F75676172")
GROUP_HEX = "150011C3C1354D619BAE9590E4D177DB7EEAF982F5BDCF78005D75157D9535FA90178F785D"


class FramingTests(unittest.TestCase):
    def test_roundtrip_all_routes_and_hash_sizes(self):
        for route in (0, 1, 2, 3):
            for hash_size in (1, 2, 3):
                for hops in (0, 1, 5, 21):
                    path = bytes(range(hops * hash_size))
                    tc = (0x1234, 0xABCD) if route in (0, 3) else None
                    raw = mc.build_packet(route, mc.PT_TXT_MSG, b"payload", path, hash_size, tc, version=1)
                    p = mc.parse_packet(raw)
                    self.assertEqual((p.route, p.payload_type, p.version, p.hash_size, p.path, p.payload, p.transport_codes),
                                     (route, mc.PT_TXT_MSG, 1, hash_size, path, b"payload", tc))
                    self.assertEqual(p.hops, hops)

    def test_limits_and_rules(self):
        with self.assertRaises(ValueError):
            mc.build_packet(1, 2, bytes(185))
        with self.assertRaises(ValueError):
            mc.build_packet(1, 2, b"", bytes(65))
        with self.assertRaises(ValueError):
            mc.build_packet(0, 2, b"")                       # transport route without codes
        with self.assertRaises(ValueError):
            mc.build_packet(1, 2, b"", transport_codes=(1, 2))
        self.assertEqual(len(mc.build_packet(1, 2, bytes(184))), 186)

    def test_parse_rejects_garbage_without_raising_anything_else(self):
        rng = random.Random(3)
        for _ in range(2000):
            raw = bytes(rng.getrandbits(8) for _ in range(rng.randrange(0, 200)))
            try:
                mc.parse_packet(raw)
            except ValueError:
                pass
            info = mc.summarize_packet(raw)                   # never raises
            self.assertIn(info["kind"], ("advert", "group_text", "other", "invalid"))
        self.assertEqual(mc.summarize_packet(b"")["kind"], "invalid")
        with self.assertRaises(ValueError):
            mc.parse_packet(bytes([0x01, 0xC0]))              # reserved hash-size code


class AdvertTests(unittest.TestCase):
    def test_reference_advert_parses_and_verifies(self):
        info = mc.summarize_packet(bytes.fromhex(ADVERT_HEX))
        self.assertEqual((info["kind"], info["route"], info["verified"]), ("advert", "Flood", True))
        self.assertEqual(info["name"], "WW7STR/PugetMesh Cougar")
        self.assertAlmostEqual(info["latitude"], 47.543968, places=6)
        self.assertAlmostEqual(info["longitude"], -122.108616, places=6)
        self.assertEqual(info["role"], "Repeater")

    def test_reference_advert_with_a_flipped_bit_fails_verification(self):
        raw = bytearray(bytes.fromhex(ADVERT_HEX))
        raw[-1] ^= 0x01                                        # name byte -> signature no longer matches
        self.assertFalse(mc.summarize_packet(bytes(raw))["verified"])

    def test_build_verify_roundtrip(self):
        ident = mc.Identity.generate()
        for route in (mc.ROUTE_FLOOD, mc.ROUTE_DIRECT):
            raw = mc.build_advert(ident, "Test Node ä", mc.ROLE_ROOM, (48.137154, 11.576124), timestamp=1_700_000_000,
                                  route=route)
            info = mc.summarize_packet(raw)
            self.assertEqual((info["kind"], info["verified"], info["name"], info["role"], info["timestamp"]),
                             ("advert", True, "Test Node ä", "Room server", 1_700_000_000))
            self.assertAlmostEqual(info["latitude"], 48.137154, places=6)
            self.assertEqual(info["public_key"], ident.public_key.hex())
            self.assertEqual(raw[0], (route) | (mc.PT_ADVERT << 2))
        no_name = mc.summarize_packet(mc.build_advert(ident, "", timestamp=5))
        self.assertEqual((no_name["verified"], no_name["name"], no_name["latitude"]), (True, "", None))

    def test_long_names_are_cut_on_a_character_boundary(self):
        info = mc.summarize_packet(mc.build_advert(mc.Identity.generate(), "ä" * 40, timestamp=1))
        self.assertTrue(info["verified"])
        self.assertEqual(info["name"], "ä" * 16)               # 32 bytes

    def test_wrong_signer_is_rejected(self):
        a, b = mc.Identity.generate(), mc.Identity.generate()
        payload = bytearray(mc.parse_packet(mc.build_advert(a, "x", timestamp=1)).payload)
        payload[:32] = b.public_key                            # key swapped, signature stays
        self.assertFalse(mc.parse_advert(bytes(payload))["signature_ok"])

    def test_advert_route_must_be_flood_or_direct(self):
        with self.assertRaises(ValueError):
            mc.build_advert(mc.Identity.generate(), "x", route=mc.ROUTE_TRANSPORT_FLOOD)


class GroupTextTests(unittest.TestCase):
    def test_reference_vector_decrypts(self):
        info = mc.summarize_packet(bytes.fromhex(GROUP_HEX))
        self.assertEqual((info["kind"], info["verified"], info["channel"]), ("group_text", True, "Public"))
        self.assertEqual((info["sender"], info["text"], info["timestamp"]), ("🌲 Tree", "☁️", 1758484279))
        self.assertEqual(mc.PUBLIC_CHANNEL.hash, 0x11)

    def test_roundtrip_and_isolation_between_channels(self):
        other = mc.GroupChannel("Test", bytes(range(16)))
        for channel in (mc.PUBLIC_CHANNEL, other):
            raw = mc.build_group_text(channel, "DA2JH", "Hallo Welt äöüß", timestamp=1_700_000_123)
            info = mc.summarize_packet(raw, (mc.PUBLIC_CHANNEL, other))
            self.assertEqual((info["kind"], info["sender"], info["text"], info["channel"]),
                             ("group_text", "DA2JH", "Hallo Welt äöüß", channel.name))
        raw = mc.build_group_text(other, "DA2JH", "secret")
        unknown = mc.summarize_packet(raw)                     # only the Public channel configured
        self.assertEqual((unknown["kind"], unknown["verified"], unknown["channel_hash"]), ("other", False, other.hash))

    def test_tampering_breaks_the_mac(self):
        raw = bytearray(mc.build_group_text(mc.PUBLIC_CHANNEL, "a", "b"))
        raw[-1] ^= 0x80
        self.assertEqual(mc.summarize_packet(bytes(raw))["kind"], "other")

    def test_length_limit(self):
        longest = mc.GROUP_TEXT_MAX_BYTES
        mc.build_group_text(mc.PUBLIC_CHANNEL, "", "x" * longest)
        with self.assertRaises(ValueError):
            mc.build_group_text(mc.PUBLIC_CHANNEL, "", "x" * (longest + 1))
        self.assertLessEqual(len(mc.build_group_text(mc.PUBLIC_CHANNEL, "", "x" * longest)), 3 + mc.MAX_PAYLOAD_BYTES)

    def test_secret_parsing(self):
        self.assertEqual(mc.parse_channel_secret("8B33 87E9 C5CD EA6A C9E5 EDBA A115 CD72"), mc.PUBLIC_CHANNEL_SECRET)
        for bad in ("", "zz", "00" * 15, "00" * 17):
            with self.assertRaises(ValueError):
                mc.parse_channel_secret(bad)


class IdentityTests(unittest.TestCase):
    def test_created_once_with_private_permissions_and_reloaded(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "sub", "id.json")
            first = meshcore_identity.load_or_create(path)
            self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
            self.assertEqual(meshcore_identity.load_or_create(path).public_key, first.public_key)
            with open(path, "w") as f:
                f.write("not json")
            self.assertNotEqual(meshcore_identity.load_or_create(path).public_key, first.public_key)  # damaged -> new

    def test_env_override(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "env.json")
            os.environ[meshcore_identity.PATH_ENV] = path
            try:
                meshcore_identity.load_or_create()
                self.assertTrue(os.path.exists(path))
            finally:
                del os.environ[meshcore_identity.PATH_ENV]


if __name__ == "__main__":
    unittest.main()
