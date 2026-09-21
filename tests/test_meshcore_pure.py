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


class DirectMessageTests(unittest.TestCase):
    def test_x25519_map_matches_a_real_firmware_keypair(self):
        # public test vector of github.com/meshcore-go/meshcore-go (MIT): a firmware-expanded private key and its public key
        from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
        from cryptography.hazmat.primitives import serialization as S
        expanded = bytes.fromhex("7065e18fd9fabb70c1ed90dca19907de698c88b709ea146eafd93d9b830c7b60"
                                 "c4681193c79bbc39945ba8064104bb618f8fd7a84a0af6f57033d6e8ddcd6471")
        public = bytes.fromhex("1ec77175b0918ed206f9ae04ec136d6d5d4315bb26305427f645b492e9350c10")
        x_pub = X25519PrivateKey.from_private_bytes(expanded[:32]).public_key().public_bytes(
            S.Encoding.Raw, S.PublicFormat.Raw)
        self.assertEqual(x_pub, mc.ed25519_public_to_x25519(public))

    def test_own_scalar_and_public_key_agree(self):
        from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
        from cryptography.hazmat.primitives import serialization as S
        for _ in range(20):
            ident = mc.Identity.generate()
            x_pub = X25519PrivateKey.from_private_bytes(ident._scalar).public_key().public_bytes(
                S.Encoding.Raw, S.PublicFormat.Raw)
            self.assertEqual(x_pub, mc.ed25519_public_to_x25519(ident.public_key))

    def test_shared_secret_is_symmetric_and_pair_specific(self):
        a, b, c = mc.Identity.generate(), mc.Identity.generate(), mc.Identity.generate()
        self.assertEqual(a.shared_secret(b.public_key), b.shared_secret(a.public_key))
        self.assertNotEqual(a.shared_secret(b.public_key), a.shared_secret(c.public_key))
        self.assertEqual(len(a.shared_secret(b.public_key)), 32)

    def test_invalid_public_keys_are_rejected(self):
        with self.assertRaises(ValueError):
            mc.ed25519_public_to_x25519(b"\x01" + bytes(31))       # y = 1: division by zero on the Montgomery map
        with self.assertRaises(ValueError):
            mc.ed25519_public_to_x25519(bytes(31))
        for bad in ("", "zz", "00" * 31, "00" * 33):
            with self.assertRaises(ValueError):
                mc.parse_public_key(bad)

    def test_roundtrip_and_isolation(self):
        a, b, c = mc.Identity.generate(), mc.Identity.generate(), mc.Identity.generate()
        raw = mc.build_text_message(a, b.public_key, "Hallo Bob äöü 🌲", timestamp=1_700_000_000, attempt=2)
        pkt = mc.parse_packet(raw)
        self.assertEqual((pkt.route, pkt.payload_type), (mc.ROUTE_DIRECT, mc.PT_TXT_MSG))
        self.assertEqual((pkt.payload[0], pkt.payload[1]), (b.node_hash, a.node_hash))
        dec = mc.decrypt_text_message(pkt.payload, b, [c.public_key, a.public_key])
        self.assertEqual((dec["text"], dec["timestamp"], dec["attempt"], dec["txt_type"], dec["sender_key"]),
                         ("Hallo Bob äöü 🌲", 1_700_000_000, 2, 0, a.public_key.hex()))
        self.assertIsNone(mc.decrypt_text_message(pkt.payload, c, [a.public_key]))        # not addressed to c
        self.assertIsNone(mc.decrypt_text_message(pkt.payload, b, [c.public_key]))        # wrong / unknown sender
        self.assertIsNone(mc.decrypt_text_message(pkt.payload, b, []))
        raw_flood = mc.build_text_message(a, b.public_key, "x", route=mc.ROUTE_FLOOD)
        self.assertEqual(mc.parse_packet(raw_flood).route, mc.ROUTE_FLOOD)

    def test_tampering_breaks_the_mac_and_hash_collisions_are_resolved(self):
        a, b = mc.Identity.generate(), mc.Identity.generate()
        payload = bytearray(mc.parse_packet(mc.build_text_message(a, b.public_key, "geheim")).payload)
        payload[-1] ^= 1
        self.assertIsNone(mc.decrypt_text_message(bytes(payload), b, [a.public_key]))
        # a second peer with the same first key byte is tried and rejected by the MAC
        other = mc.Identity.generate()
        while other.node_hash != a.node_hash:
            other = mc.Identity.generate()
        good = mc.parse_packet(mc.build_text_message(a, b.public_key, "ok")).payload
        self.assertEqual(mc.decrypt_text_message(good, b, [other.public_key, a.public_key])["text"], "ok")

    def test_length_limit_and_empty_text(self):
        a, b = mc.Identity.generate(), mc.Identity.generate()
        mc.build_text_message(a, b.public_key, "x" * mc.DIRECT_TEXT_MAX_BYTES)
        with self.assertRaises(ValueError):
            mc.build_text_message(a, b.public_key, "x" * (mc.DIRECT_TEXT_MAX_BYTES + 1))

    def test_summary_kinds(self):
        a, b, c = mc.Identity.generate(), mc.Identity.generate(), mc.Identity.generate()
        raw = mc.build_text_message(a, b.public_key, "hi")
        s_for_b = mc.summarize_packet(raw, identity=b, peers=[a.public_key])
        self.assertEqual((s_for_b["kind"], s_for_b["verified"], s_for_b["text"]), ("direct_text", True, "hi"))
        s_no_peer = mc.summarize_packet(raw, identity=b, peers=[])
        self.assertEqual((s_no_peer["kind"], s_no_peer["verified"], s_no_peer["for_this_node"]), ("other", False, True))
        s_other = mc.summarize_packet(raw, identity=c, peers=[a.public_key])
        self.assertEqual((s_other["kind"], s_other["for_this_node"], s_other["dest_hash"], s_other["src_hash"]),
                         ("other", False, b.node_hash, a.node_hash))
        self.assertEqual(mc.summarize_packet(raw)["kind"], "other")                     # no identity: never decrypted
        ack = mc.summarize_packet(mc.build_packet(mc.ROUTE_DIRECT, mc.PT_ACK, b"\x01\x02\x03\x04"))
        self.assertEqual(ack["ack_hash"], "01020304")


class StateTests(unittest.TestCase):
    def test_direct_message_before_the_advert_is_decrypted_afterwards(self):
        from pluto_advanced_rx.meshcore_state import MeshcoreState
        a, me = mc.Identity.generate(), mc.Identity.generate()
        state = MeshcoreState()
        state.set_identity(me)
        state.on_frame(mc.build_text_message(a, me.public_key, "zuerst die Nachricht"))
        self.assertEqual(state.get_snapshot()[1][0]["kind"], "other")
        state.on_frame(mc.build_advert(a, "Alice"))
        kinds = [r["kind"] for r in state.get_snapshot()[1]]
        self.assertEqual(kinds, ["direct_text", "advert"])
        self.assertEqual(state.get_snapshot()[1][0]["text"], "zuerst die Nachricht")

    def test_without_identity_nothing_is_decrypted_and_switching_off_works(self):
        from pluto_advanced_rx.meshcore_state import MeshcoreState
        a, me = mc.Identity.generate(), mc.Identity.generate()
        state = MeshcoreState()
        state.on_frame(mc.build_advert(a, "Alice"))
        state.on_frame(mc.build_text_message(a, me.public_key, "x"))
        self.assertEqual(state.get_snapshot()[1][1]["kind"], "other")
        state.set_identity(me)
        state.on_frame(mc.build_text_message(a, me.public_key, "y"))
        self.assertEqual(state.get_snapshot()[1][2]["kind"], "direct_text")
        state.set_identity(None)
        state.on_frame(mc.build_text_message(a, me.public_key, "z"))
        self.assertEqual(state.get_snapshot()[1][3]["kind"], "other")


if __name__ == "__main__":
    unittest.main()
