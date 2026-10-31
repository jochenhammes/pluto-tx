"""Generates vectors.jsonl: random Extended Squitter messages with what pyModeS (independent decoder, 2.22.0) reads from
them. Not part of the build, only its output is committed. Run: python gen_vectors.py <path to repo> > vectors.jsonl"""
import json
import random
import sys

import pyModeS as pms

sys.path.insert(0, sys.argv[1])
from pluto_tx.mode_s_crc import crc24  # noqa: E402

rng = random.Random(0x1090)


def message(tc, me_rest_bits):
    head = (17 << 3 | 5).to_bytes(1, "big") + rng.getrandbits(24).to_bytes(3, "big")
    body = head + ((tc << 51) | me_rest_bits).to_bytes(7, "big")
    return (body + crc24(body).to_bytes(3, "big")).hex().upper()


def emit(hexm, **expected):
    print(json.dumps({"message": hexm, **expected}))


for _ in range(100):
    m = message(rng.randint(1, 4), rng.getrandbits(51))
    emit(m, callsign=pms.adsb.callsign(m).replace("_", "").replace("#", ""))
for _ in range(200):
    m = message(rng.randint(9, 18), rng.getrandbits(51))
    emit(m, alt_ft=pms.adsb.altitude(m), odd=pms.adsb.oe_flag(m))
for _ in range(300):
    sub = rng.choice((1, 2, 3, 4))
    m = message(19, sub << 48 | rng.getrandbits(48))
    v = pms.adsb.velocity(m)
    if v is None:
        continue
    emit(m, speed=v[0], direction=v[1], vrate=v[2], speed_type=v[3], subtype=sub)
for _ in range(100):
    m = message(28, 1 << 48 | rng.getrandbits(48))
    emit(m, emergency=pms.adsb.emergency_state(m), squawk=pms.adsb.emergency_squawk(m))
for _ in range(150):
    ref = (rng.uniform(-80, 80), rng.uniform(-179, 179))
    m = message(rng.randint(5, 8), rng.getrandbits(51))
    v = pms.adsb.surface_velocity(m)
    p = pms.adsb.surface_position_with_ref(m, *ref)
    emit(m, surface_speed=v[0], surface_track=v[1], reference=ref, surface_position=p)
for _ in range(300):
    # pairs from a real position, encoded with CPR by hand: the even and odd message pyModeS must locate
    lat, lon = rng.uniform(-85, 85), rng.uniform(-179.9, 179.9)
    alt = rng.randint(0, 40000 // 25) * 25

    def encode(odd):
        d_lat = 360.0 / (60 - odd)
        y = int((lat % d_lat) / d_lat * 131072 + 0.5) & 0x1FFFF
        r = d_lat * (int(lat // d_lat) + y / 131072)
        nl = pms.cprNL(r)
        zones = max(nl - odd, 1)
        d_lon = 360.0 / zones
        x = int((lon % d_lon) / d_lon * 131072 + 0.5) & 0x1FFFF
        return y, x

    def pos_message(odd):
        y, x = encode(odd)
        n = (alt + 1000) // 25
        code = (n >> 4) << 5 | 0x10 | n & 0xF
        return message(11, code << 36 | odd << 34 | y << 17 | x)

    even, odd = pos_message(0), pos_message(1)
    newest_odd = rng.random() < 0.5
    t_even, t_odd = (0, 1) if newest_odd else (1, 0)
    p = pms.adsb.position(even, odd, t_even, t_odd)
    emit(even, odd_message=odd, newest_odd=newest_odd, position=p, true_position=(lat, lon), alt_ft=alt)
