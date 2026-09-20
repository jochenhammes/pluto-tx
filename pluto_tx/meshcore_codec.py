"""MeshCore packet layer (pure Python, no GNU Radio): packet framing, Advert build/verify (Ed25519) and
group-text encryption (AES-128-ECB + 2-byte HMAC-SHA256). Shared by the TX flowgraph and the RX state.

Sources: MeshCore docs/packet_format.md and docs/payloads.md, src/Utils.cpp (encrypt-then-MAC), and the
public reference vector of the meshcore-decoder project (Public channel GroupText, see tests). All integers
are little-endian.

Packet:  [header 1][transport codes 4 (route 0 and 3 only)][path_len 1][path][payload]
header:  0bVVPPPPRR = version (bits 6-7), payload type (bits 2-5), route type (bits 0-1)
path_len: bits 0-5 hop count, bits 6-7 hash size code (0 = 1 byte, 1 = 2, 2 = 3)
Advert:  [public key 32][timestamp 4][signature 64][appdata]; signature = Ed25519 over key || timestamp || appdata
GRP_TXT: [channel hash 1][mac 2][AES-128-ECB(secret, zero padded (timestamp 4 | flags 1 | "sender: text"))]
"""
import hashlib
import hmac
import struct
from dataclasses import dataclass, field

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives import serialization

ROUTE_TRANSPORT_FLOOD, ROUTE_FLOOD, ROUTE_DIRECT, ROUTE_TRANSPORT_DIRECT = 0, 1, 2, 3
ROUTE_NAMES = {0: "Transport-Flood", 1: "Flood", 2: "Direct", 3: "Transport-Direct"}

PT_REQ, PT_RESPONSE, PT_TXT_MSG, PT_ACK, PT_ADVERT, PT_GRP_TXT, PT_GRP_DATA, PT_ANON_REQ = 0, 1, 2, 3, 4, 5, 6, 7
PT_PATH, PT_TRACE, PT_MULTIPART, PT_CONTROL, PT_RAW_CUSTOM = 8, 9, 10, 11, 15
PAYLOAD_TYPE_NAMES = {
    0: "REQ", 1: "RESPONSE", 2: "TXT_MSG", 3: "ACK", 4: "ADVERT", 5: "GRP_TXT", 6: "GRP_DATA", 7: "ANON_REQ",
    8: "PATH", 9: "TRACE", 10: "MULTIPART", 11: "CONTROL", 15: "RAW_CUSTOM",
}

MAX_PATH_BYTES = 64
MAX_PAYLOAD_BYTES = 184
PUB_KEY_SIZE = 32
SIGNATURE_SIZE = 64
CIPHER_MAC_SIZE = 2

# The well-known secret of the pre-configured "Public" channel (channel hash 0x11).
PUBLIC_CHANNEL_SECRET = bytes.fromhex("8b3387e9c5cdea6ac9e5edbaa115cd72")

ROLE_CHAT, ROLE_REPEATER, ROLE_ROOM, ROLE_SENSOR = 1, 2, 3, 4
ROLE_NAMES = {0: "unknown", 1: "Chat", 2: "Repeater", 3: "Room server", 4: "Sensor"}
FLAG_LOCATION, FLAG_FEATURE1, FLAG_FEATURE2, FLAG_NAME = 0x10, 0x20, 0x40, 0x80

NAME_MAX_BYTES = 32  # keeps the advert well under the 184-byte payload limit


# ---------------------------------------------------------------- packet framing

@dataclass
class Packet:
    route: int
    payload_type: int
    version: int = 0
    transport_codes: tuple = None
    hash_size: int = 1
    path: bytes = b""
    payload: bytes = b""

    @property
    def hops(self):
        return len(self.path) // self.hash_size if self.hash_size else 0

    @property
    def path_hashes(self):
        n = self.hash_size
        return [self.path[i:i + n] for i in range(0, len(self.path), n)]

    @property
    def type_name(self):
        return PAYLOAD_TYPE_NAMES.get(self.payload_type, f"0x{self.payload_type:X}")

    @property
    def route_name(self):
        return ROUTE_NAMES[self.route]


def build_packet(route, payload_type, payload, path=b"", hash_size=1, transport_codes=None, version=0) -> bytes:
    if route not in ROUTE_NAMES:
        raise ValueError(f"bad route type {route}")
    if not 0 <= payload_type <= 15:
        raise ValueError(f"bad payload type {payload_type}")
    if hash_size not in (1, 2, 3):
        raise ValueError("hash size must be 1, 2 or 3 bytes")
    if len(path) % hash_size or len(path) > MAX_PATH_BYTES or len(path) // hash_size > 63:
        raise ValueError("bad path length")
    if len(payload) > MAX_PAYLOAD_BYTES:
        raise ValueError(f"payload longer than {MAX_PAYLOAD_BYTES} bytes")
    has_transport = route in (ROUTE_TRANSPORT_FLOOD, ROUTE_TRANSPORT_DIRECT)
    if has_transport != (transport_codes is not None):
        raise ValueError("transport codes are required for route types 0 and 3, and only for those")
    out = bytearray([((version & 3) << 6) | (payload_type << 2) | route])
    if has_transport:
        out += struct.pack("<HH", *transport_codes)
    out.append(((hash_size - 1) << 6) | (len(path) // hash_size))
    out += path
    out += payload
    return bytes(out)


def parse_packet(raw: bytes) -> Packet:
    """Raises ValueError for anything that is not a well-formed MeshCore packet."""
    if len(raw) < 2:
        raise ValueError("packet too short")
    header = raw[0]
    route, ptype, version = header & 3, (header >> 2) & 15, header >> 6
    i = 1
    transport = None
    if route in (ROUTE_TRANSPORT_FLOOD, ROUTE_TRANSPORT_DIRECT):
        if len(raw) < i + 5:
            raise ValueError("truncated transport codes")
        transport = struct.unpack_from("<HH", raw, i)
        i += 4
    path_len = raw[i]
    i += 1
    code, hops = path_len >> 6, path_len & 63
    if code == 3:
        raise ValueError("reserved hash size")
    hash_size = code + 1
    n = hops * hash_size
    if n > MAX_PATH_BYTES or len(raw) < i + n:
        raise ValueError("bad path")
    path = bytes(raw[i:i + n])
    payload = bytes(raw[i + n:])
    if len(payload) > MAX_PAYLOAD_BYTES:
        raise ValueError("payload too long")
    return Packet(route, ptype, version, transport, hash_size, path, payload)


# ---------------------------------------------------------------- identity / adverts

class Identity:
    """An Ed25519 node identity (32-byte seed -> 32-byte public key)."""

    def __init__(self, seed: bytes):
        if len(seed) != 32:
            raise ValueError("seed must be 32 bytes")
        self.seed = bytes(seed)
        self._key = Ed25519PrivateKey.from_private_bytes(self.seed)
        self.public_key = self._key.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw)

    @classmethod
    def generate(cls):
        return cls(Ed25519PrivateKey.generate().private_bytes(
            serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption()))

    def sign(self, message: bytes) -> bytes:
        return self._key.sign(message)


def build_appdata(role=ROLE_CHAT, name="", location=None) -> bytes:
    """Flags, optional location (lat, lon in degrees), name (UTF-8, at most NAME_MAX_BYTES)."""
    flags = role & 0x0F
    body = b""
    if location is not None:
        lat, lon = location
        flags |= FLAG_LOCATION
        body += struct.pack("<ii", round(lat * 1_000_000), round(lon * 1_000_000))
    raw_name = name.encode("utf-8")
    if len(raw_name) > NAME_MAX_BYTES:  # cut on a character boundary
        raw_name = raw_name[:NAME_MAX_BYTES].decode("utf-8", "ignore").encode("utf-8")
    if raw_name:
        flags |= FLAG_NAME
        body += raw_name
    return bytes([flags]) + body


def build_advert_payload(identity: Identity, timestamp: int, appdata: bytes) -> bytes:
    ts = struct.pack("<I", timestamp & 0xFFFFFFFF)
    signature = identity.sign(identity.public_key + ts + appdata)
    return identity.public_key + ts + signature + appdata


def build_advert(identity: Identity, name="", role=ROLE_CHAT, location=None, timestamp=None, route=ROUTE_FLOOD) -> bytes:
    import time as _time
    if route not in (ROUTE_FLOOD, ROUTE_DIRECT):
        raise ValueError("an advert is sent as Flood or Direct")
    ts = int(_time.time()) if timestamp is None else int(timestamp)
    payload = build_advert_payload(identity, ts, build_appdata(role, name, location))
    return build_packet(route, PT_ADVERT, payload)


def parse_advert(payload: bytes) -> dict:
    """-> dict(public_key, timestamp, signature, signature_ok, flags, role, name, latitude, longitude, ...).
    Raises ValueError if the payload is too short; signature_ok is False for a bad signature."""
    if len(payload) < PUB_KEY_SIZE + 4 + SIGNATURE_SIZE + 1:
        raise ValueError("advert too short")
    key = bytes(payload[:32])
    ts_raw = bytes(payload[32:36])
    signature = bytes(payload[36:100])
    appdata = bytes(payload[100:])
    try:
        Ed25519PublicKey.from_public_bytes(key).verify(signature, key + ts_raw + appdata)
        ok = True
    except (InvalidSignature, ValueError):
        ok = False
    flags = appdata[0]
    i = 1
    out = dict(public_key=key, timestamp=struct.unpack("<I", ts_raw)[0], signature=signature, signature_ok=ok,
               flags=flags, role=flags & 0x0F, name="", latitude=None, longitude=None, features=(None, None))
    if flags & FLAG_LOCATION and len(appdata) >= i + 8:
        lat, lon = struct.unpack_from("<ii", appdata, i)
        out["latitude"], out["longitude"] = lat / 1e6, lon / 1e6
        i += 8
    f1 = f2 = None
    if flags & FLAG_FEATURE1 and len(appdata) >= i + 2:
        f1 = struct.unpack_from("<H", appdata, i)[0]
        i += 2
    if flags & FLAG_FEATURE2 and len(appdata) >= i + 2:
        f2 = struct.unpack_from("<H", appdata, i)[0]
        i += 2
    out["features"] = (f1, f2)
    if flags & FLAG_NAME:
        out["name"] = appdata[i:].decode("utf-8", "replace")
    return out


# ---------------------------------------------------------------- group channels

def channel_hash(secret: bytes) -> int:
    return hashlib.sha256(secret).digest()[0]


def _aes_ecb(secret: bytes, data: bytes, encrypt: bool) -> bytes:
    c = Cipher(algorithms.AES(secret[:16]), modes.ECB())
    op = c.encryptor() if encrypt else c.decryptor()
    return op.update(data) + op.finalize()


def _mac(secret: bytes, ciphertext: bytes) -> bytes:
    return hmac.new(secret, ciphertext, hashlib.sha256).digest()[:CIPHER_MAC_SIZE]


@dataclass
class GroupChannel:
    name: str
    secret: bytes = field(repr=False)

    def __post_init__(self):
        if len(self.secret) != 16:
            raise ValueError("channel secret must be 16 bytes (32 hex digits)")

    @property
    def hash(self) -> int:
        return channel_hash(self.secret)


PUBLIC_CHANNEL = GroupChannel("Public", PUBLIC_CHANNEL_SECRET)


def parse_channel_secret(text: str) -> bytes:
    text = text.strip().replace(" ", "")
    try:
        raw = bytes.fromhex(text)
    except ValueError:
        raise ValueError("channel secret must be 32 hex digits") from None
    if len(raw) != 16:
        raise ValueError("channel secret must be 32 hex digits")
    return raw


# plaintext limit: the ciphertext (16-byte blocks) has to fit MAX_PAYLOAD_BYTES - 1 (hash) - 2 (mac)
GROUP_PLAINTEXT_MAX = ((MAX_PAYLOAD_BYTES - 1 - CIPHER_MAC_SIZE) // 16) * 16
GROUP_TEXT_MAX_BYTES = GROUP_PLAINTEXT_MAX - 5  # minus timestamp (4) and flags (1); includes "sender: "


def build_group_text_payload(channel: GroupChannel, sender: str, text: str, timestamp: int, flags: int = 0) -> bytes:
    body = f"{sender}: {text}".encode("utf-8") if sender else text.encode("utf-8")
    if len(body) > GROUP_TEXT_MAX_BYTES:
        raise ValueError(f"message is longer than {GROUP_TEXT_MAX_BYTES} bytes (including the sender name)")
    plain = struct.pack("<IB", timestamp & 0xFFFFFFFF, flags & 0xFF) + body
    plain += bytes(-len(plain) % 16)
    cipher = _aes_ecb(channel.secret, plain, True)
    return bytes([channel.hash]) + _mac(channel.secret, cipher) + cipher


def build_group_text(channel: GroupChannel, sender: str, text: str, timestamp=None, route=ROUTE_FLOOD) -> bytes:
    import time as _time
    ts = int(_time.time()) if timestamp is None else int(timestamp)
    return build_packet(route, PT_GRP_TXT, build_group_text_payload(channel, sender, text, ts))


def decrypt_group_text(payload: bytes, channels) -> dict:
    """Try every channel whose hash byte matches. -> dict(channel, timestamp, flags, sender, text) or None
    (unknown channel or bad MAC)."""
    if len(payload) < 1 + CIPHER_MAC_SIZE + 16 or (len(payload) - 1 - CIPHER_MAC_SIZE) % 16:
        return None
    ch_hash, mac, cipher = payload[0], bytes(payload[1:3]), bytes(payload[3:])
    for channel in channels:
        if channel.hash != ch_hash or not hmac.compare_digest(_mac(channel.secret, cipher), mac):
            continue
        plain = _aes_ecb(channel.secret, cipher, False)
        ts, flags = struct.unpack_from("<IB", plain)
        body = plain[5:].rstrip(b"\x00").decode("utf-8", "replace")
        sender, sep, message = body.partition(": ")
        if not sep:
            sender, message = "", body
        return dict(channel=channel.name, timestamp=ts, flags=flags, sender=sender, text=message)
    return None


# ---------------------------------------------------------------- summary (never raises)

def summarize_packet(raw: bytes, channels=(PUBLIC_CHANNEL,)) -> dict:
    """One dict per received frame for the RX table: kind 'advert' | 'group_text' | 'other' | 'invalid'.
    Never raises. 'verified' is True only where the content authenticated itself (advert signature,
    group MAC); other payload types carry no integrity information a receiver can check without keys."""
    info = dict(kind="invalid", raw_len=len(raw), verified=False, hex=bytes(raw).hex())
    try:
        pkt = parse_packet(raw)
    except ValueError as e:
        info["error"] = str(e)
        return info
    info.update(kind="other", route=pkt.route_name, type=pkt.type_name, hops=pkt.hops,
                path=[h.hex().upper() for h in pkt.path_hashes], version=pkt.version, payload_len=len(pkt.payload))
    try:
        if pkt.payload_type == PT_ADVERT:
            adv = parse_advert(pkt.payload)
            info.update(kind="advert", verified=adv["signature_ok"], name=adv["name"], role=ROLE_NAMES.get(adv["role"], "?"),
                        public_key=adv["public_key"].hex(), timestamp=adv["timestamp"],
                        latitude=adv["latitude"], longitude=adv["longitude"])
        elif pkt.payload_type == PT_GRP_TXT:
            dec = decrypt_group_text(pkt.payload, channels)
            if dec is not None:
                info.update(kind="group_text", verified=True, **dec)
            else:
                info["channel_hash"] = pkt.payload[0] if pkt.payload else None
    except ValueError:
        info["kind"] = "invalid"
    return info
