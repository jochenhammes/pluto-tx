"""ADS-B (1090 MHz Extended Squitter, DF17/DF18) message decoding. Pure Python, no GNU Radio; receive only -- this
project does not transmit on 1090 MHz, not even for testing (the test encoder in tests/ only ever makes files).

A message is 112 bits: DF[5] CA[3] ICAO[24] ME[56] PI[24]. Everything is big endian, bits numbered from the top of each
field. See docs/adsb/SPEC.md. Position is CPR-encoded: one message does not locate an aircraft by itself, cpr_global()
needs one even and one odd message, cpr_local() a reference position nearby.
"""
import math

from pluto_tx.mode_s_crc import crc24

MESSAGE_BYTES = 14
ADSB_DOWNLINK_FORMATS = (17, 18)
_CALLSIGN_CHARS = "#ABCDEFGHIJKLMNOPQRSTUVWXYZ##### ###############0123456789######"
_CPR_MAX = 131072.0  # 2^17
_NZ = 15
EMERGENCIES = {0: "none", 1: "general", 2: "medical", 3: "minimum fuel", 4: "no communications", 5: "unlawful interference",
               6: "downed aircraft", 7: "reserved"}


def downlink_format(message: bytes) -> int:
    return message[0] >> 3


def _bits(value: int, total: int, first: int, count: int) -> int:
    """`count` bits of a `total`-bit field, `first` counted from its top starting at 1, as the specifications do."""
    return (value >> (total - first - count + 1)) & ((1 << count) - 1)


def _callsign(me: int) -> str:
    return "".join(_CALLSIGN_CHARS[_bits(me, 56, 9 + 6 * i, 6)] for i in range(8)).replace("#", "").strip()


def _altitude_ft(code: int) -> int | None:
    """12-bit barometric altitude. Q=0 (Gillham, only below about 50000 ft in old transponders) is reported as unknown."""
    if not code or not code & 0x10:
        return None
    return ((code >> 5) << 4 | code & 0x0F) * 25 - 1000


def _ground_speed_kt(code: int) -> float | None:
    """The movement field of a surface position (DO-260B table 2-16)."""
    for limit, base, step, start in ((1, None, 0, 0), (2, 0.0, 0, 1), (9, 0.125, 0.125, 2), (13, 1.0, 0.25, 9),
                                     (39, 2.0, 0.5, 13), (94, 15.0, 1.0, 39), (109, 70.0, 2.0, 94), (124, 100.0, 5.0, 109)):
        if code < limit:
            return None if base is None else base + (code - start) * step
    return 175.0 if code == 124 else None


def _squawk(code: int) -> str:
    """13-bit identity code C1 A1 C2 A2 C4 A4 X B1 D1 B2 D2 B4 D4 -> four octal digits ABCD."""
    def bit(n):
        return (code >> (13 - n)) & 1
    a = bit(2) << 0 | bit(4) << 1 | bit(6) << 2
    b = bit(8) << 0 | bit(10) << 1 | bit(12) << 2
    c = bit(1) << 0 | bit(3) << 1 | bit(5) << 2
    d = bit(9) << 0 | bit(11) << 1 | bit(13) << 2
    return f"{a}{b}{c}{d}"


def _velocity(me: int) -> dict:
    subtype = _bits(me, 56, 6, 3)
    info = {"kind": "velocity"}
    vr = _bits(me, 56, 38, 9)
    if vr:
        info["vrate_fpm"] = (vr - 1) * 64 * (-1 if _bits(me, 56, 37, 1) else 1)
    else:
        info["vrate_fpm"] = None
    if subtype in (1, 2):
        ew, ns = _bits(me, 56, 15, 10), _bits(me, 56, 26, 10)
        if not ew or not ns:  # zero means "no information", not "standing still"
            return info
        scale = 4 if subtype == 2 else 1
        vx = (ew - 1) * scale * (-1 if _bits(me, 56, 14, 1) else 1)
        vy = (ns - 1) * scale * (-1 if _bits(me, 56, 25, 1) else 1)
        info["speed_kt"] = math.hypot(vx, vy)
        info["speed_type"] = "ground"
        info["track_deg"] = math.degrees(math.atan2(vx, vy)) % 360
    elif subtype in (3, 4):
        if _bits(me, 56, 14, 1):
            info["heading_deg"] = _bits(me, 56, 15, 10) * 360 / 1024
        airspeed = _bits(me, 56, 26, 10)
        if airspeed:
            info["speed_kt"] = float((airspeed - 1) * (4 if subtype == 4 else 1))
            info["speed_type"] = "true air" if _bits(me, 56, 25, 1) else "indicated air"
    return info


def _position(me: int) -> dict:
    return {"cpr_odd": bool(_bits(me, 56, 22, 1)), "cpr_lat": _bits(me, 56, 23, 17), "cpr_lon": _bits(me, 56, 40, 17)}


def parse_message(message: bytes) -> dict | None:
    """One 14-byte message whose parity already holds. Returns its fields, or None for something that is not ADS-B."""
    if len(message) != MESSAGE_BYTES or downlink_format(message) not in ADSB_DOWNLINK_FORMATS:
        return None
    df = downlink_format(message)
    capability = message[0] & 7
    if df == 18 and capability > 1:  # DF18 CF 2..7 is TIS-B/ADS-R: traffic someone else is relaying
        return None
    me = int.from_bytes(message[4:11], "big")
    tc = me >> 51
    info = {"df": df, "icao": message[1:4].hex().upper(), "type_code": tc}
    if 1 <= tc <= 4:
        info.update(kind="identification", callsign=_callsign(me))
    elif 5 <= tc <= 8:
        info.update(kind="surface", on_ground=True, **_position(me))
        info["speed_kt"] = _ground_speed_kt(_bits(me, 56, 6, 7))
        info["speed_type"] = "ground"
        info["track_deg"] = _bits(me, 56, 14, 7) * 360 / 128 if _bits(me, 56, 13, 1) else None
    elif 9 <= tc <= 18:
        info.update(kind="position", on_ground=False, alt_ft=_altitude_ft(_bits(me, 56, 9, 12)), alt_type="baro", **_position(me))
    elif 20 <= tc <= 22:
        metres = _bits(me, 56, 9, 12)
        info.update(kind="position", on_ground=False, alt_ft=round(metres / 0.3048) if metres else None, alt_type="gnss",
                    **_position(me))
    elif tc == 19:
        info.update(_velocity(me))
    elif tc == 28 and _bits(me, 56, 6, 3) == 1:
        info.update(kind="status", emergency=EMERGENCIES[_bits(me, 56, 9, 3)], squawk=_squawk(_bits(me, 56, 12, 13)))
    else:
        info["kind"] = "other"
    return info


def check_message(message: bytes) -> bool:
    return len(message) == MESSAGE_BYTES and crc24(message) == 0


def _nl(lat: float) -> int:
    """Number of longitude zones at a latitude."""
    if abs(lat) >= 87:
        return 2 if abs(lat) == 87 else 1
    if lat == 0:
        return 59
    a = 1 - math.cos(math.pi / (2 * _NZ))
    b = math.cos(math.pi / 180 * abs(lat)) ** 2
    return int(math.floor(2 * math.pi / math.acos(1 - a / b)))


def cpr_global(even: tuple[int, int], odd: tuple[int, int], newest_is_odd: bool) -> tuple[float, float] | None:
    """Airborne position from one even and one odd (lat, lon) pair, at most 10 s apart. None if the pair straddles a
    longitude-zone boundary, which makes it undecodable."""
    lat_e, lon_e = even[0] / _CPR_MAX, even[1] / _CPR_MAX
    lat_o, lon_o = odd[0] / _CPR_MAX, odd[1] / _CPR_MAX
    j = math.floor(59 * lat_e - 60 * lat_o + 0.5)
    r_e = 360.0 / 60 * ((j % 60) + lat_e)
    r_o = 360.0 / 59 * ((j % 59) + lat_o)
    r_e = r_e - 360 if r_e >= 270 else r_e
    r_o = r_o - 360 if r_o >= 270 else r_o
    if _nl(r_e) != _nl(r_o):
        return None
    lat = r_o if newest_is_odd else r_e
    nl = _nl(lat)
    m = math.floor(lon_e * (nl - 1) - lon_o * nl + 0.5)
    zones = max(nl - 1 if newest_is_odd else nl, 1)
    lon = 360.0 / zones * ((m % zones) + (lon_o if newest_is_odd else lon_e))
    lon = lon - 360 if lon >= 180 else lon
    return (lat, lon) if -90 <= lat <= 90 else None


def cpr_local(cpr_lat: int, cpr_lon: int, odd: bool, reference: tuple[float, float], surface: bool = False) -> tuple[float, float]:
    """Position from one message and a reference within half a CPR zone (about 180 NM airborne, 45 NM on the surface)."""
    full = 90.0 if surface else 360.0
    y, x = cpr_lat / _CPR_MAX, cpr_lon / _CPR_MAX
    d_lat = full / (60 - int(odd))
    j = math.floor(reference[0] / d_lat) + math.floor(0.5 + (reference[0] % d_lat) / d_lat - y)
    lat = d_lat * (j + y)
    zones = max(_nl(lat) - int(odd), 1)
    d_lon = full / zones
    m = math.floor(reference[1] / d_lon) + math.floor(0.5 + (reference[1] % d_lon) / d_lon - x)
    return lat, d_lon * (m + x)


def locator_center(locator: str) -> tuple[float, float] | None:
    """Middle of a Maidenhead locator (4 or 6 characters) as (lat, lon), north and east positive; None if not valid."""
    loc = (locator or "").strip().upper()
    if len(loc) not in (4, 6) or not ("A" <= loc[0] <= "R" and "A" <= loc[1] <= "R" and loc[2:4].isdigit()):
        return None
    lon = (ord(loc[0]) - 65) * 20 + int(loc[2]) * 2 - 180
    lat = (ord(loc[1]) - 65) * 10 + int(loc[3]) - 90
    if len(loc) == 6:
        if not ("A" <= loc[4] <= "X" and "A" <= loc[5] <= "X"):
            return None
        return lat + (ord(loc[5]) - 65 + 0.5) / 24, lon + (ord(loc[4]) - 65 + 0.5) / 12
    return lat + 0.5, lon + 1.0


def distance_km(a: tuple[float, float], b: tuple[float, float]) -> float:
    lat1, lat2 = math.radians(a[0]), math.radians(b[0])
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(math.radians(b[1] - a[1]) / 2) ** 2
    return 2 * 6371 * math.asin(min(1.0, math.sqrt(h)))
