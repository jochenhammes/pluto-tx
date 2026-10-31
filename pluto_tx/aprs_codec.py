"""APRS over AX.25: frames and the APRS 1.0.1 information field, both directions. Pure Python, no GNU Radio; shared by the
transmit side (aprs.py) and the receiver (pluto_advanced_rx/aprs_demod.py). See docs/aprs/SPEC.md.

Whatever arrives from the air may be malformed: parse_info() never raises, and a packet it cannot read comes back as
kind "other" with its text, rather than as a guess.
"""
import re

MAX_DIGIPEATERS = 8
MAX_MESSAGE_TEXT = 67
MIN_FRAME_BYTES = 14 + 2 + 2  # two addresses, control + PID, FCS
MAX_FRAME_BYTES = 400
_CALL_RE = re.compile(r"^[A-Z0-9]{1,6}(-(?:[0-9]|1[0-5]))?$")
_MESSAGE_FORBIDDEN = set("|~{")


def fcs16(data: bytes) -> int:
    """CRC-16/X.25, as AX.25 uses it."""
    crc = 0xFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ 0x8408 if crc & 1 else crc >> 1
    return crc ^ 0xFFFF


def valid_callsign(call: str) -> bool:
    return bool(_CALL_RE.match(call))


def _address(call: str, last: bool, repeated=False) -> bytes:
    if not valid_callsign(call):
        raise ValueError(f"invalid callsign {call!r}")
    name, _, ssid = call.partition("-")
    out = bytearray(ord(c) << 1 for c in name.ljust(6))
    out.append(0x60 | (0x80 if repeated else 0) | int(ssid or 0) << 1 | int(last))
    return bytes(out)


def build_frame(source: str, destination: str, path, info: bytes) -> bytes:
    """A complete AX.25 UI frame with its FCS."""
    path = list(path)
    if len(path) > MAX_DIGIPEATERS:
        raise ValueError(f"at most {MAX_DIGIPEATERS} digipeaters")
    body = _address(destination, False) + _address(source, not path)
    for i, hop in enumerate(path):
        body += _address(hop, i == len(path) - 1)
    body += bytes((0x03, 0xF0)) + info
    return body + fcs16(body).to_bytes(2, "little")


def parse_frame(frame: bytes) -> dict | None:
    """-> {"source", "destination", "path", "info", "heard_via"}, or None unless the FCS holds and it is a UI frame."""
    if not MIN_FRAME_BYTES <= len(frame) <= MAX_FRAME_BYTES or fcs16(frame[:-2]) != int.from_bytes(frame[-2:], "little"):
        return None
    addresses = []
    pos = 0
    while True:
        if pos + 7 > len(frame) - 2 or len(addresses) >= MAX_DIGIPEATERS + 2:
            return None
        raw = frame[pos:pos + 7]
        if any(b & 1 for b in raw[:6]):
            return None
        name = "".join(chr(b >> 1) for b in raw[:6]).rstrip()
        if not name or not name.isalnum():
            return None
        ssid = (raw[6] >> 1) & 0x0F
        addresses.append((f"{name}-{ssid}" if ssid else name, bool(raw[6] & 0x80)))
        pos += 7
        if raw[6] & 1:
            break
    if len(addresses) < 2 or pos + 2 > len(frame) - 2 or frame[pos] != 0x03 or frame[pos + 1] != 0xF0:
        return None
    path = [f"{a}*" if used else a for a, used in addresses[2:]]
    used = [a for a, was in addresses[2:] if was]
    return {"destination": addresses[0][0], "source": addresses[1][0], "path": path, "info": frame[pos + 2:-2],
            "heard_via": used[-1] if used else None}


def hdlc_bits(frame: bytes, preamble_flags=40, tail_flags=4) -> list:
    """Bits in air order: flags without stuffing, the frame LSB first with a 0 stuffed after five 1s."""
    flag = [0, 1, 1, 1, 1, 1, 1, 0]
    bits = flag * preamble_flags
    ones = 0
    for byte in frame:
        for i in range(8):
            bit = (byte >> i) & 1
            bits.append(bit)
            ones = ones + 1 if bit else 0
            if ones == 5:
                bits.append(0)
                ones = 0
    return bits + flag * tail_flags


# --- APRS information field --------------------------------------------------------------------------------------------

_POSITION = re.compile(r"^(\d{2})([\d ]{2})\.([\d ]{2})([NS])(.)(\d{3})([\d ]{2})\.([\d ]{2})([EW])(.)")
_COURSE_SPEED = re.compile(r"^(\d{3})/(\d{3})")
_ALTITUDE = re.compile(r"/A=(\d{6})")
_WEATHER_FIELD = re.compile(r"([cstgrpPhbL])(-?[\d.]+)")


def _decimal(degrees: str, minutes: str, hundredths: str, sign: bool) -> float | None:
    # Spaces mean deliberate position ambiguity: the middle of the stated area is the honest value.
    minutes_text = minutes.replace(" ", "5") + "." + hundredths.replace(" ", "5")
    value = int(degrees) + float(minutes_text) / 60
    return -value if sign else value


def _base91(text: str) -> int:
    value = 0
    for c in text:
        value = value * 91 + ord(c) - 33
    return value


def _comment_fields(comment: str) -> dict:
    out = {}
    match = _COURSE_SPEED.match(comment)
    if match and not (match[1] == "000" and match[2] == "000"):
        out["course_deg"] = int(match[1]) % 360
        out["speed_kt"] = float(match[2])
        comment = comment[7:]
    altitude = _ALTITUDE.search(comment)
    if altitude:
        out["alt_ft"] = int(altitude[1])
        comment = comment[:altitude.start()] + comment[altitude.end():]
    out["comment"] = comment.strip()
    return out


def _uncompressed(text: str) -> dict | None:
    match = _POSITION.match(text)
    if not match:
        return None
    lat = _decimal(match[1], match[2], match[3], match[4] == "S")
    lon = _decimal(match[6], match[7], match[8], match[9] == "W")
    if lat is None or lon is None or abs(lat) > 90 or abs(lon) > 180:
        return None
    info = {"lat": lat, "lon": lon, "symbol": match[5] + match[10]}
    info.update(_comment_fields(text[19:]))
    return info


def _compressed(text: str) -> dict | None:
    if len(text) < 13 or not all(33 <= ord(c) <= 123 for c in text[1:9]):
        return None
    lat = 90 - _base91(text[1:5]) / 380926
    lon = -180 + _base91(text[5:9]) / 190463
    if abs(lat) > 90 or abs(lon) > 180:
        return None
    info = {"lat": lat, "lon": lon, "symbol": text[0] + text[9]}
    c, s, t = ord(text[10]) - 33, ord(text[11]) - 33, ord(text[12]) - 33
    if text[10] != " ":
        if t & 0x18 == 0x10:
            info["alt_ft"] = round(1.002 ** (c * 91 + s))
        elif 0 <= c <= 89:
            info["course_deg"] = (c * 4) % 360
            info["speed_kt"] = round(1.08 ** s - 1, 1)
    info["comment"] = text[13:].strip()
    return info


def _position(text: str) -> dict | None:
    if not text:
        return None
    if text[0].isdigit():
        return _uncompressed(text)
    if text[0] in "/\\" or text[0].isupper() or text[0] in "abcdefghij":
        return _compressed(text)
    return None


def _mice_digit(c: str) -> str | None:
    if "0" <= c <= "9":
        return c
    if "A" <= c <= "J":
        return chr(ord(c) - 17)
    if "P" <= c <= "Y":
        return chr(ord(c) - 32)
    return "5" if c in "KLZ" else None  # ambiguity: middle of the digit's range


def _mice(info: bytes, destination: str) -> dict | None:
    dest = destination.split("-")[0]
    if len(dest) != 6 or len(info) < 9:
        return None
    digits = [_mice_digit(c) for c in dest]
    if None in digits:
        return None
    lat = int(digits[0] + digits[1]) + float(digits[2] + digits[3] + "." + digits[4] + digits[5]) / 60
    if dest[3] < "L":
        lat = -lat
    degrees = info[1] - 28 + (100 if dest[4] >= "L" else 0)
    if 180 <= degrees <= 189:
        degrees -= 80
    elif 190 <= degrees <= 199:
        degrees -= 190
    minutes = info[2] - 28
    if minutes >= 60:
        minutes -= 60
    lon = degrees + (minutes + (info[3] - 28) / 100) / 60
    if dest[5] >= "L":
        lon = -lon
    if abs(lat) > 90 or abs(lon) > 180:
        return None
    speed = (info[4] - 28) * 10 + (info[5] - 28) // 10
    course = ((info[5] - 28) % 10) * 100 + info[6] - 28
    out = {"kind": "position", "lat": lat, "lon": lon, "symbol": chr(info[8]) + chr(info[7]),
           "speed_kt": float(speed - 800 if speed >= 800 else speed), "course_deg": (course - 400 if course >= 400 else course) % 360}
    comment = info[9:].decode("latin-1")
    if len(comment) >= 4 and comment[3] == "}":
        out["alt_ft"] = round((_base91(comment[:3]) - 10000) / 0.3048)
        comment = comment[4:]
    out["comment"] = comment.strip()
    return out


def _weather(text: str) -> dict:
    out = {}
    wind = re.match(r"^(\d{3})/(\d{3})", text)
    if wind:
        out["wind_dir_deg"] = int(wind[1])
        out["wind_mph"] = float(wind[2])
    for key, value in _WEATHER_FIELD.findall(text):
        try:
            number = float(value)
        except ValueError:
            continue
        if key == "t":
            out["temperature_c"] = round((number - 32) * 5 / 9, 1)
        elif key == "g":
            out["gust_mph"] = number
        elif key == "h":
            out["humidity_pct"] = 100.0 if number == 0 else number
        elif key == "b":
            out["pressure_hpa"] = number / 10
    return out


def parse_info(info: bytes, destination: str = "") -> dict:
    """The information field as a dict with "kind": position, weather, status, object, item, message, ack, reject, bulletin,
    telemetry or other. Never raises; `text` always holds the whole field."""
    text = info.decode("utf-8", "replace") if info else ""
    out = {"kind": "other", "text": text}
    try:
        out.update(_parse_info(info, text, destination) or {})
    except (ValueError, IndexError, OverflowError):
        pass
    return out


def _parse_info(info: bytes, text: str, destination: str) -> dict | None:
    if not text:
        return None
    dti = text[0]
    if dti in "!=":
        if len(text) > 1 and text[1:2].isdigit() or text[1:2] in ("/", "\\") or text[1:2].isupper():
            return _position_or_weather(_position(text[1:]))
    elif dti in "/@":
        return _position_or_weather(_position(text[8:]), timestamp=text[1:8])
    elif dti in "`'\x1c\x1d":
        return _mice(info, destination)
    elif dti == ";" and len(text) >= 18:
        position = _position(text[18:])
        return {"kind": "object", "name": text[1:10].strip(), "alive": text[10] == "*", "timestamp": text[11:18], **(position or {})}
    elif dti == ")":
        match = re.match(r"^\)([^!_]{3,9})([!_])", text)
        if match:
            position = _position(text[match.end():])
            return {"kind": "item", "name": match[1].strip(), "alive": match[2] == "!", **(position or {})}
    elif dti == ":" and len(text) >= 11 and text[10] == ":":
        return _message(text[1:10].strip(), text[11:])
    elif dti == ">":
        return {"kind": "status", "comment": text[1:].strip()}
    elif dti == "_" and len(text) >= 9:
        return {"kind": "weather", **_weather(text[9:])}
    elif dti == "T" and text.startswith("T#"):
        return {"kind": "telemetry"}
    elif dti == "}":
        return _third_party(text[1:])
    return None


def _position_or_weather(position, timestamp=None) -> dict | None:
    if position is None:
        return None
    position["kind"] = "position"
    if timestamp:
        position["timestamp"] = timestamp
    if position["symbol"][1] == "_":  # the weather-station symbol: the comment carries the readings
        position.update(_weather(position.get("comment", "")))
        if "course_deg" in position:  # at a weather station, ccc/sss is the wind
            position["wind_dir_deg"] = position.pop("course_deg")
            position["wind_mph"] = position.pop("speed_kt")
        position["kind"] = "weather"
    return position


def _message(addressee: str, body: str) -> dict:
    if body.startswith("ack") and len(body) <= 8:
        return {"kind": "ack", "addressee": addressee, "message_id": body[3:].rstrip("}")}
    if body.startswith("rej") and len(body) <= 8:
        return {"kind": "reject", "addressee": addressee, "message_id": body[3:].rstrip("}")}
    if addressee.startswith("BLN") or addressee.startswith("NWS"):
        return {"kind": "bulletin", "addressee": addressee, "message": body}
    text, _, number = body.partition("{")
    return {"kind": "message", "addressee": addressee, "message": text, "message_id": number.split("}")[0] or None}


def _third_party(text: str) -> dict | None:
    header, _, payload = text.partition(":")
    source = header.split(">")[0]
    destination = header.split(">")[1].split(",")[0] if ">" in header else ""
    if not valid_callsign(source.upper()):
        return None
    inner = parse_info(payload.encode("utf-8", "replace"), destination)
    inner.pop("text", None)
    return {**inner, "third_party_source": source}


# --- Building what we send --------------------------------------------------------------------------------------------

def _encode_position(lat: float, lon: float, symbol: str) -> str:
    if not -90 <= lat <= 90 or not -180 <= lon <= 180:
        raise ValueError("position out of range")
    if len(symbol) != 2 or symbol[0] not in "/\\" and not symbol[0].isalnum():
        raise ValueError("symbol must be a table character and a code character")
    lat_min, lon_min = abs(lat) * 60, abs(lon) * 60
    lat_d, lat_m = int(lat_min // 60), round(lat_min % 60, 2)
    lon_d, lon_m = int(lon_min // 60), round(lon_min % 60, 2)
    if lat_m >= 60:
        lat_d, lat_m = lat_d + 1, 0.0
    if lon_m >= 60:
        lon_d, lon_m = lon_d + 1, 0.0
    return (f"{lat_d:02d}{lat_m:05.2f}{'N' if lat >= 0 else 'S'}{symbol[0]}"
            f"{lon_d:03d}{lon_m:05.2f}{'E' if lon >= 0 else 'W'}{symbol[1]}")


def _check_text(text: str, what: str, limit: int) -> None:
    if len(text) > limit:
        raise ValueError(f"{what} is too long: at most {limit} characters")
    if any(not 32 <= ord(c) <= 126 for c in text):
        raise ValueError(f"{what} may only hold printable ASCII")


def beacon_info(lat: float, lon: float, symbol: str, comment: str = "") -> bytes:
    _check_text(comment, "comment", 43)
    return ("!" + _encode_position(lat, lon, symbol) + comment).encode("ascii")


def message_info(addressee: str, text: str, message_id: str | None = None) -> bytes:
    if not valid_callsign(addressee):
        raise ValueError(f"invalid addressee {addressee!r}")
    if not text:
        raise ValueError("message text is empty")
    _check_text(text, "message text", MAX_MESSAGE_TEXT)
    if set(text) & _MESSAGE_FORBIDDEN:
        raise ValueError("message text must not contain | ~ {")
    return f":{addressee:<9}:{text}".encode("ascii") + (f"{{{message_id}".encode("ascii") if message_id else b"")


def ack_info(addressee: str, message_id: str) -> bytes:
    if not valid_callsign(addressee):
        raise ValueError(f"invalid addressee {addressee!r}")
    return f":{addressee:<9}:ack{message_id}".encode("ascii")


def summarize(frame: dict) -> dict:
    """A received frame as JSON-ready fields for state, CLI and browser."""
    parsed = parse_info(frame["info"], frame["destination"])
    return {"source": frame["source"], "destination": frame["destination"], "path": frame["path"],
            "heard_via": frame["heard_via"], **parsed}
