"""ADS-L (EASA ADS-L 4 SRD860 Issue 2) packet layer: CRC-24, XXTEA descrambling and the Traffic payload.

Pure Python, no GNU Radio: used by the RX burst demodulator (pluto_advanced_rx/adsl_demod.py). Receive
only -- this project does not transmit ADS-L; the encoder needed for testing lives in tests/adsl_testgen.py.

Frame (M-band, after Manchester decoding): length byte (number of bytes that follow, without any
signature), network header (version[4] signature[1] key index[2] error control[1], LSB first), ADS-L data
(5-byte header + payload, a whole number of 32-bit words, XXTEA-scrambled with key 0), parity (CRC-24
sent MSB first, computed over network header and scrambled data). Integers are little endian; field
offsets count from the least significant bit of each byte. See docs/adsl/SPEC.md.
"""
import struct

SYNC_BYTES = bytes((0x72, 0x4B))
TRAFFIC_LENGTH = 0x18  # network header + 20 data bytes + CRC-24
MIN_LENGTH = 12  # network header + 5-byte header + 3-byte minimum payload + CRC-24
MAX_LENGTH = 255

TYPE_TRAFFIC = 2
PAYLOAD_TYPES = {2: "traffic", 3: "status", 4: "traffic-uplink", 5: "fisb-uplink", 6: "remote-id", 66: "ogn-diagnostics"}
ADDRESS_TABLES = {0: "RND", 5: "ICAO", 6: "FLARM", 7: "OGN", 8: "FANET"}
FLIGHT_STATES = {0: "undefined", 1: "ground", 2: "airborne", 3: "reserved"}
CATEGORIES = {
    0: "unknown", 1: "light fixed wing", 2: "small-heavy fixed wing", 3: "light rotorcraft", 4: "glider",
    5: "lighter-than-air", 6: "ultralight", 7: "paraglider", 8: "parachutist", 9: "eVTOL/UAM", 10: "gyrocopter",
    11: "UAS open", 12: "UAS specific", 13: "UAS certified", 14: "model plane", 15: "heavy rotorcraft",
    16: "hang-glider", 17: "paramotor",
}
EMERGENCIES = {
    0: "undefined", 1: "none", 2: "general", 3: "medical", 4: "no communications", 5: "unlawful interference",
    6: "downed aircraft", 7: "reserved",
}
KEY_ZERO = 0
KEY_NONE = 3  # scrambling disabled
_LAT_LSB_PER_DEG = 93206
_LON_LSB_PER_DEG = 46603
_NO_POSITION = (0x800000, 0xFFFFFF)  # Issue 2 and Issue 1 respectively
_XXTEA_ROUNDS = 6
_XXTEA_DELTA = 0x9E3779B9
_MASK32 = 0xFFFFFFFF
_CRC24_POLY = 0xFFF409  # the Mode-S generator without its x^24 term


def crc24(data: bytes) -> int:
    """Mode-S CRC-24: running it over data plus its own three CRC bytes gives 0."""
    crc = 0
    for byte in data:
        crc ^= byte << 16
        for _ in range(8):
            crc = ((crc << 1) ^ _CRC24_POLY if crc & 0x800000 else crc << 1) & 0xFFFFFF
    return crc


def _mx(y, z, total):
    return ((((z >> 5) ^ (y << 2)) + ((y >> 3) ^ (z << 4))) ^ ((total ^ y) + z)) & _MASK32


def xxtea_descramble(data: bytes) -> bytes:
    """Reverse XXTEA with the all-zero key (6 rounds) over a whole number of little-endian 32-bit words."""
    words = list(struct.unpack(f"<{len(data) // 4}I", data))
    last = len(words) - 1
    total = (_XXTEA_ROUNDS * _XXTEA_DELTA) & _MASK32
    y = words[0]
    for _ in range(_XXTEA_ROUNDS):
        for p in range(last, 0, -1):
            words[p] = y = (words[p] - _mx(y, words[p - 1], total)) & _MASK32
        words[0] = y = (words[0] - _mx(y, words[last], total)) & _MASK32
        total = (total - _XXTEA_DELTA) & _MASK32
    return struct.pack(f"<{len(words)}I", *words)


def _exp_decode(raw: int, base_bits: int) -> int:
    """value = 2^exponent * (2^N + base) - 2^N, the exponent being the two bits above the N-bit base."""
    exponent = raw >> base_bits
    base = raw & ((1 << base_bits) - 1)
    return (1 << exponent) * ((1 << base_bits) + base) - (1 << base_bits)


def _coordinate(raw: bytes, lsb_per_deg: int):
    value = int.from_bytes(raw, "little")
    if value in _NO_POSITION:
        return None
    return (value - (1 << 24) if value & 0x800000 else value) / lsb_per_deg


def decode_traffic(payload: bytes) -> dict:
    """The 15-byte Traffic payload (Subpart G.1). A field that is marked invalid decodes to None."""
    speed_raw = payload[8]
    alt_raw = payload[9] | (payload[10] & 0x3F) << 8
    climb_raw = payload[10] >> 6 | (payload[11] & 0x7F) << 2
    track_raw = payload[11] >> 7 | payload[12] << 1
    speed = None if speed_raw == 0xFF else _exp_decode(speed_raw, 6) * 0.25
    climb = None
    if climb_raw != 0x1FF:
        climb = _exp_decode(climb_raw & 0xFF, 6) * 0.125
        if climb_raw & 0x100:
            climb = -climb
    lat = _coordinate(payload[2:5], _LAT_LSB_PER_DEG)
    lon = _coordinate(payload[5:8], _LON_LSB_PER_DEG)
    return {
        "timestamp_s": (payload[0] & 0x3F) * 0.25,
        "flight_state": FLIGHT_STATES[payload[0] >> 6],
        "category": CATEGORIES.get(payload[1] & 0x1F, "reserved"),
        "emergency": EMERGENCIES[payload[1] >> 5],
        "lat": None if lon is None else lat,
        "lon": None if lat is None else lon,
        "speed_mps": speed,
        "alt_m": None if alt_raw == 0x3FFF else _exp_decode(alt_raw, 12) - 320,
        "climb_mps": climb,
        "track_deg": None if not speed else track_raw * 360 / 512,  # invalid if speed is zero or invalid
        "source_integrity": payload[13] & 0x03,
        "design_assurance": payload[13] >> 2 & 0x03,
        "navigation_integrity": payload[13] >> 4,
        "horizontal_accuracy": payload[14] & 0x07,
        "vertical_accuracy": payload[14] >> 3 & 0x03,
        "velocity_accuracy": payload[14] >> 5 & 0x03,
    }


def valid_length(length: int) -> bool:
    return MIN_LENGTH <= length <= MAX_LENGTH


def parse_frame(frame: bytes):
    """frame: length byte + `length` further bytes. Returns the packet as a dict, or None unless the frame is
    complete and its CRC holds. Frames using Reed-Solomon parity are not decoded yet (None, see below)."""
    if len(frame) < 1 or not valid_length(frame[0]) or len(frame) != frame[0] + 1:
        return None
    body = frame[1:]
    if body[0] & 0x80:  # error control mode 1 = 32 Reed-Solomon parity bytes, used for long uplinks
        return None
    if crc24(body) != 0:
        return None
    version = body[0] & 0x0F
    key_index = body[0] >> 5 & 0x03
    data = body[1:-3]
    info = {"version": version, "key_index": key_index, "signed": bool(body[0] & 0x10), "raw": frame.hex()}
    if key_index == KEY_ZERO:
        if len(data) % 4:
            return None
        clear = xxtea_descramble(data)
    elif key_index == KEY_NONE:
        clear = data
    else:  # keys 1 and 2 are reserved: the CRC holds but the content is not readable
        info["kind"] = "encrypted"
        return info
    address_word = int.from_bytes(clear[1:5], "little")
    table = address_word & 0x3F
    info.update(
        payload_type=clear[0],
        kind=PAYLOAD_TYPES.get(clear[0], "unknown"),
        address_table=table,
        address_table_name=ADDRESS_TABLES.get(table, f"M{table}"),
        address=address_word >> 6 & 0xFFFFFF,
        relayed=bool(address_word >> 31),
    )
    if clear[0] == TYPE_TRAFFIC and version <= 1 and len(clear) == 20:
        info.update(decode_traffic(clear[5:]))
    return info


def address_label(info: dict) -> str:
    return f"{info['address_table_name']}:{info['address']:06X}"


def event_fields(info: dict) -> dict:
    """A received packet as JSON-ready fields for pluto-cli's and Web-TRX's adsl_packet event."""
    fields = {k: v for k, v in info.items() if k not in ("inverted", "signed", "address_table")}
    if "address" in info:
        fields["address"] = f"{info['address']:06X}"
        fields["address_label"] = address_label(info)
    return fields
