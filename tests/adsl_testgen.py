"""Test-only ADS-L transmit side: frame encoder and GFSK baseband modulator, to feed the receiver with
synthetic IQ. Deliberately kept out of the application: this project never transmits ADS-L."""
import struct

import numpy as np

from pluto_tx import adsl_codec as c

CHIP_RATE = 100_000
DEVIATION_HZ = 50_000
BT = 0.5
_ROUNDS = 6
_DELTA = 0x9E3779B9
_M = 0xFFFFFFFF


def xxtea_scramble(data: bytes) -> bytes:
    words = list(struct.unpack(f"<{len(data) // 4}I", data))
    total = 0
    z = words[-1]
    for _ in range(_ROUNDS):
        total = (total + _DELTA) & _M
        for p in range(len(words)):
            y = words[(p + 1) % len(words)]
            mx = (((z >> 5) ^ (y << 2)) + ((y >> 3) ^ (z << 4))) ^ ((total ^ y) + z)
            words[p] = z = (words[p] + mx) & _M
    return struct.pack(f"<{len(words)}I", *words)


def exp_encode(value: int, base_bits: int) -> int:
    n = 1 << base_bits
    for exponent in (3, 2, 1, 0):
        if value >= (n << exponent) - n:
            return min(exponent << base_bits | (value + n - (n << exponent)) >> exponent, (4 << base_bits) - 2)
    return 0


def traffic_clear(address=0x123456, table=8, timestamp=16, state=2, category=4, emergency=1, lat=48.12345,
                  lon=8.12345, speed_mps=45.0, alt_m=1234, climb_mps=-5.5, track_raw=256, relayed=False) -> bytes:
    speed = exp_encode(round(speed_mps * 4), 6)
    alt = exp_encode(alt_m + 320, 12)
    climb = exp_encode(round(abs(climb_mps) * 8), 6) | (0x100 if climb_mps < 0 else 0)
    head = bytes((c.TYPE_TRAFFIC,)) + (table | address << 6 | int(relayed) << 31).to_bytes(4, "little")
    payload = bytes((timestamp | state << 6, category | emergency << 5))
    payload += (round(lat * 93206) & 0xFFFFFF).to_bytes(3, "little")
    payload += (round(lon * 46603) & 0xFFFFFF).to_bytes(3, "little")
    payload += bytes((speed, alt & 0xFF, alt >> 8 | (climb & 0x03) << 6, climb >> 2 | (track_raw & 1) << 7, track_raw >> 1))
    return head + payload + bytes(2)


def frame(clear: bytes, version=0, key_index=0) -> bytes:
    data = xxtea_scramble(clear) if key_index == 0 else clear
    body = bytes((version | key_index << 5,)) + data
    return bytes((len(body) + 3,)) + body + c.crc24(body).to_bytes(3, "big")


def manchester_chips(data: bytes) -> list:
    chips = []
    for byte in data:
        for i in range(7, -1, -1):
            chips += [0, 1] if byte >> i & 1 else [1, 0]
    return chips


def air_chips(frame_bytes: bytes, preamble_repeats=8) -> list:
    return [0, 1] * preamble_repeats + [1, 0, 0, 1, 1, 0, 0, 1] + manchester_chips(c.SYNC_BYTES + frame_bytes)


def modulate(chips, rate, invert=False) -> np.ndarray:
    """Unit-amplitude GFSK at sample rate `rate` (any ratio to the chip rate) with its own short lead-in/out."""
    guard = 8
    levels = np.concatenate([np.ones(guard) * (2 * chips[0] - 1), 2.0 * np.array(chips) - 1, np.ones(guard) * (2 * chips[-1] - 1)])
    n = int(len(levels) * rate / CHIP_RATE)
    t_chips = np.arange(n) * CHIP_RATE / rate
    held = levels[np.minimum(t_chips.astype(int), len(levels) - 1)]
    sigma = np.sqrt(np.log(2)) / (2 * np.pi * BT) * rate / CHIP_RATE
    half = max(int(4 * sigma), 1)
    x = np.arange(-half, half + 1)
    gauss = np.exp(-0.5 * (x / sigma) ** 2)
    gauss /= gauss.sum()
    freq = np.convolve(held, gauss, mode="same") * DEVIATION_HZ * (-1 if invert else 1)
    return np.exp(2j * np.pi * np.cumsum(freq) / rate).astype(np.complex64)
