"""Meshtastic mesh-packet encode/decode -- Phase 2 of the LoRa mesh plan
(/home/hammesj/.claude/plans/swirling-waddling-noodle.md), protocol layer
tested independent of the LoRa CSS PHY (pluto_tx/lora.py, Phase 1). Wraps
the official `meshtastic` PyPI package's protobuf definitions
(meshtastic.protobuf.mesh_pb2) directly at the protobuf layer, NOT its
device-interface classes -- those are for live serial/BLE communication
with a real Meshtastic device, not raw-byte decoding of a payload this
project's own LoRa PHY already extracted from the air. Requires the
`meshtastic` and `cryptography` pip packages (installed --user, see
install-lora.sh's own comment for why pip is used here specifically --
unlike gr-lora_sdr, this is a pure-Python protocol library with no native
build, a normal PyPI install is the right tool, not a from-source build).

## Real, from-source-verified facts (not assumed or guessed)

Over-the-air packet = 16-byte PacketHeader + AES-{128,256}-CTR-encrypted,
protobuf-serialized `Data` message. PacketHeader layout (fetched from
Meshtastic firmware's own src/mesh/RadioInterface.h `PacketHeader` struct
this session):
    to:         4 bytes, little-endian uint32 (destination NodeNum)
    from:       4 bytes, little-endian uint32 (sender NodeNum)
    id:         4 bytes, little-endian uint32 (packet ID)
    flags:      1 byte  (bits 0-2: hop_limit, bit 3: want_ack,
                          bit 4: via_mqtt, bits 5-7: hop_start)
    channel:    1 byte  (channel hash -- a hint for the decoder, NOT
                          itself the encryption key)
    next_hop:   1 byte  (last byte of next-hop NodeNum)
    relay_node: 1 byte  (last byte of relaying NodeNum)

AES-CTR nonce (16 bytes, fetched from CryptoEngine.cpp's initNonce()
this session): packetId zero-extended to 8 bytes (little-endian) +
fromNode (4 bytes, little-endian) + extraNonce (4 bytes, 0 for a normal
packet, little-endian) = 16 bytes total (one AES block, as CTR mode
needs).

**Default/"LongFast" channel PSK is AES-128, NOT AES-256** -- this
project's own earlier plan draft assumed AES-256-CTR for the default
channel; that was wrong, corrected here after fetching the real value.
The 1-byte shorthand PSK (0x01, base64 "AQ==") firmware expands to the
full base64 "1PG7OiApB1nwvP+rz05pAQ==", which decodes to exactly 16
bytes (d4f1bb3a20290759f0bcffabcf4e6901) -- Meshtastic's own PSK-length
convention (0=no crypto, 16=AES128, 32=AES256) means the stock/default
channel most real nearby Meshtastic nodes use out of the box is
AES-128-CTR. AES-256 only applies if an operator sets their OWN 32-byte
custom channel PSK. `cryptography.hazmat.primitives.ciphers.algorithms.
AES` picks AES-128 vs AES-256 automatically from the key length passed
in, so both are supported by the same code path here -- just pass a
32-byte psk for a custom AES-256 channel."""
import struct

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from meshtastic.protobuf import mesh_pb2

DEFAULT_CHANNEL_PSK = bytes.fromhex("d4f1bb3a20290759f0bcffabcf4e6901")

# The PacketHeader "channel" byte is NOT arbitrary/cosmetic -- real
# Meshtastic firmware uses it as a fast PRE-FILTER (checked before ever
# attempting AES decryption) to guess which locally-configured channel a
# received packet belongs to. Formula (firmware Channels.cpp generateHash):
# h = xorHash(channel_name_bytes) ^ xorHash(psk_bytes), xorHash = plain
# XOR of all bytes. xorHash(DEFAULT_CHANNEL_PSK) = 0x02.
#
# CORRECTED 2026-09-19 against REAL packets: an earlier version of this
# comment (and value, 0x02) assumed the default primary channel's stored
# name is the EMPTY string. Real packets from a stock Heltec V3 carry
# channel byte 0x08, and xorHash("LongFast") ^ 0x02 == 0x0A ^ 0x02 == 0x08
# exactly -- so on real devices the default channel IS stored under the
# name "LongFast". A wrong hash byte risks the packet being dropped by a
# real receiver's pre-filter before decryption is tried.
DEFAULT_CHANNEL_NAME = "LongFast"


def channel_hash(name: str, psk: bytes) -> int:
    h = 0
    for b in name.encode("utf-8") + psk:
        h ^= b
    return h


DEFAULT_CHANNEL_HASH = channel_hash(DEFAULT_CHANNEL_NAME, DEFAULT_CHANNEL_PSK)  # 0x08

BROADCAST_ADDR = 0xFFFFFFFF
HEADER_LEN = 16
_HEADER_STRUCT = struct.Struct("<IIIBBBB")  # to, from, id, flags, channel, next_hop, relay_node


def _build_nonce(packet_id: int, from_node: int, extra_nonce: int = 0) -> bytes:
    return struct.pack("<QII", packet_id, from_node, extra_nonce)


def _ctr_crypt(data: bytes, psk: bytes, packet_id: int, from_node: int) -> bytes:
    """AES-CTR is its own inverse (XOR-based keystream) -- one function
    for both encrypt and decrypt, matching CryptoEngine's own design."""
    nonce = _build_nonce(packet_id, from_node)
    cipher = Cipher(algorithms.AES(psk), modes.CTR(nonce))
    ctx = cipher.encryptor()  # CTR mode: encryptor/decryptor are identical operations
    return ctx.update(data) + ctx.finalize()


def encode_packet(to: int, from_: int, packet_id: int, data: mesh_pb2.Data,
                   channel_hash: int = DEFAULT_CHANNEL_HASH, hop_limit: int = 3, want_ack: bool = False,
                   via_mqtt: bool = False, hop_start: int = 3, relay_node=None,
                   psk: bytes = DEFAULT_CHANNEL_PSK) -> bytes:
    """Build a real, over-the-air-shaped Meshtastic packet: 16-byte
    PacketHeader + AES-CTR-encrypted serialized Data protobuf. `psk`
    must be 0 (no crypto -- payload sent as plaintext protobuf, no AES
    call at all), 16 (AES-128), or 32 (AES-256) bytes, matching
    Meshtastic's own convention."""
    plaintext = data.SerializeToString()
    ciphertext = plaintext if len(psk) == 0 else _ctr_crypt(plaintext, psk, packet_id, from_)
    flags = (hop_limit & 0x07) | (0x08 if want_ack else 0) | (0x10 if via_mqtt else 0) \
        | ((hop_start & 0x07) << 5)
    # real packets: next_hop = 0 (no preferred next hop), relay_node = low
    # byte of the transmitting node's own number (0xCC for node 0x43b59fcc,
    # observed in real Heltec V3 traffic)
    relay = (from_ & 0xFF) if relay_node is None else relay_node
    header = _HEADER_STRUCT.pack(to, from_, packet_id, flags, channel_hash, 0, relay)
    return header + ciphertext


def decode_packet(raw: bytes, psk: bytes = DEFAULT_CHANNEL_PSK) -> dict:
    """Inverse of encode_packet(). Returns a dict of header fields plus
    the decoded mesh_pb2.Data payload under "data". Raises ValueError
    (too short) or google.protobuf.message.DecodeError (garbage
    payload / wrong PSK) on malformed input -- callers should expect
    both on real, possibly-corrupted or wrong-channel over-the-air
    data, not just trust every packet decodes cleanly."""
    if len(raw) < HEADER_LEN:
        raise ValueError(f"packet too short: {len(raw)} bytes, need >= {HEADER_LEN}")
    to, from_, packet_id, flags, channel_hash, next_hop, relay_node = _HEADER_STRUCT.unpack(
        raw[:HEADER_LEN]
    )
    ciphertext = raw[HEADER_LEN:]
    plaintext = ciphertext if len(psk) == 0 else _ctr_crypt(ciphertext, psk, packet_id, from_)

    data = mesh_pb2.Data()
    data.ParseFromString(plaintext)

    return {
        "to": to, "from": from_, "id": packet_id,
        "hop_limit": flags & 0x07, "want_ack": bool(flags & 0x08),
        "via_mqtt": bool(flags & 0x10), "hop_start": (flags >> 5) & 0x07,
        "channel_hash": channel_hash, "next_hop": next_hop, "relay_node": relay_node,
        "data": data,
    }
