"""Test-only ADS-B side: message encoder and PPM baseband generator, to feed the receiver with synthetic IQ from memory
and files. Deliberately not in the application: this project never transmits on 1090 MHz, and nothing here may ever
be connected to a transmitter."""
import numpy as np

from pluto_tx.mode_s_crc import crc24

CPR_MAX = 131072


def encode_message(icao: int, tc: int, me_rest: int, df=17, capability=5) -> bytes:
    body = bytes((df << 3 | capability,)) + icao.to_bytes(3, "big") + ((tc << 51) | me_rest).to_bytes(7, "big")
    return body + crc24(body).to_bytes(3, "big")


def callsign_message(icao, text, category=3):
    chars = "#ABCDEFGHIJKLMNOPQRSTUVWXYZ##### ###############0123456789######"
    me = category << 48
    for i, c in enumerate(text.ljust(8)[:8]):
        me |= chars.index(c) << (42 - 6 * i)
    return encode_message(icao, 4, me)


def _nl(lat):
    a = 1 - np.cos(np.pi / 30)
    b = np.cos(np.radians(abs(lat))) ** 2
    if abs(lat) >= 87:
        return 1
    return 59 if lat == 0 else int(np.floor(2 * np.pi / np.arccos(1 - a / b)))


def position_message(icao, lat, lon, alt_ft, odd):
    d_lat = 360.0 / (60 - odd)
    y = int((lat % d_lat) / d_lat * CPR_MAX + 0.5) & 0x1FFFF
    r = d_lat * (np.floor(lat / d_lat) + y / CPR_MAX)
    d_lon = 360.0 / max(_nl(r) - odd, 1)
    x = int((lon % d_lon) / d_lon * CPR_MAX + 0.5) & 0x1FFFF
    n = (alt_ft + 1000) // 25
    code = (n >> 4) << 5 | 0x10 | n & 0xF
    return encode_message(icao, 11, code << 36 | odd << 34 | y << 17 | x)


def velocity_message(icao, east_kt, north_kt, vrate_fpm=0):
    def v(x):
        return (1 if x < 0 else 0) << 10 | (abs(int(x)) + 1)
    vr = (1 if vrate_fpm < 0 else 0) << 9 | (abs(vrate_fpm) // 64 + 1)
    return encode_message(icao, 19, 1 << 48 | v(east_kt) << 32 | v(north_kt) << 21 | vr << 10)


def ppm_power(message: bytes, rate: float, pad_us=2.0) -> np.ndarray:
    """Amplitude (0..1) of the on-air pulse train at `rate`: a 0.5 us pulse at 0, 1, 3.5, 4.5 us, then per bit
    a pulse in the first (1) or second (0) half of its microsecond, starting at 8 us."""
    starts = [0.0, 1.0, 3.5, 4.5]
    for i in range(len(message) * 8):
        bit = (message[i // 8] >> (7 - i % 8)) & 1
        starts.append(8.0 + i + (0.0 if bit else 0.5))
    total = int((8 + len(message) * 8 + 2 * pad_us) * rate / 1e6) + 2
    edges = (np.arange(total + 1)) / (rate / 1e6) - pad_us
    level = np.zeros(total)
    for start in starts:  # share of each sample covered by the pulse
        overlap = np.minimum(edges[1:], start + 0.5) - np.maximum(edges[:-1], start)
        level += np.maximum(overlap, 0) * (rate / 1e6)
    return np.minimum(level, 1.0)


def iq(message, rate, phase_rad=0.0, freq_offset_hz=0.0, amplitude=1.0):
    a = ppm_power(message, rate) * amplitude
    return a * np.exp(1j * (phase_rad + 2 * np.pi * freq_offset_hz * np.arange(len(a)) / rate))
