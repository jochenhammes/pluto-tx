"""Generates vectors.jsonl: APRS packets in all position formats and what aprslib (independent parser) reads from them. Not
part of the build, only its output is committed. Run: python gen_vectors.py > vectors.jsonl"""
import json
import random

import aprslib

rng = random.Random(0x4150)
B91 = lambda v, n: "".join(chr(33 + (v // 91 ** (n - 1 - i)) % 91) for i in range(n))  # noqa: E731


def uncompressed():
    lat, lon = rng.uniform(-85, 85), rng.uniform(-179, 179)
    la, lo = abs(lat) * 60, abs(lon) * 60
    text = f"{int(la // 60):02d}{la % 60:05.2f}{'N' if lat >= 0 else 'S'}/{int(lo // 60):03d}{lo % 60:05.2f}{'E' if lon >= 0 else 'W'}>"
    if rng.random() < 0.5:
        text += f"{rng.randint(1, 360):03d}/{rng.randint(0, 200):03d}"
    if rng.random() < 0.5:
        text += f"/A={rng.randint(0, 40000):06d}"
    return "!" + text + "test", "WIDE1-1", "APRS"


def compressed():
    lat, lon = rng.uniform(-85, 85), rng.uniform(-179, 179)
    y, x = round((90 - lat) * 380926), round((lon + 180) * 190463)
    cs = chr(33 + rng.randint(0, 89)) + chr(33 + rng.randint(0, 80)) + "G"
    return "!/" + B91(y, 4) + B91(x, 4) + ">" + cs + "test", "WIDE2-1", "APRS"


def mice():
    lat, lon = rng.uniform(0, 85), rng.uniform(0, 179)
    deg, minutes = int(lat), (lat - int(lat)) * 60
    digits = f"{deg:02d}{int(minutes):02d}{round((minutes % 1) * 100) % 100:02d}"
    west, north = rng.random() < 0.5, rng.random() < 0.5
    lond, lonm = int(lon), (lon - int(lon)) * 60
    offset = lond >= 100 or lond < 10
    dest = ""
    for i, d in enumerate(digits):
        use_pz = (i < 3 and rng.random() < 0.5) or (i == 3 and north) or (i == 4 and offset) or (i == 5 and west)
        dest += chr(ord(d) + 32) if use_pz else d
    d28 = lond if not offset else (lond - 100 if lond >= 100 else lond + 80)
    if d28 < 0:
        d28 += 100
    m28 = int(lonm)
    h28 = round((lonm % 1) * 100) % 100
    speed, course = rng.randint(0, 199), rng.randint(0, 359)
    info = "`" + chr(d28 + 28) + chr(m28 + 28) + chr(h28 + 28)
    info += chr(speed // 10 + 28) + chr((speed % 10) * 10 + course // 100 + 28) + chr(course % 100 + 28) + ">/"
    return info + "test", "WIDE1-1", dest


def emit(info, path, dest):
    raw = f"DA2JH-9>{dest},{path}:{info}"
    try:
        p = aprslib.parse(raw)
    except (aprslib.ParseError, aprslib.UnknownFormat):
        return
    keep = {k: p[k] for k in ("format", "latitude", "longitude", "course", "speed", "altitude", "addresse", "message_text", "msgNo", "response", "comment", "symbol", "symbol_table", "status") if k in p}
    print(json.dumps({"info": info, "destination": dest, "aprslib": keep}, ensure_ascii=False))


for _ in range(150):
    emit(*uncompressed())
for _ in range(100):
    emit(*compressed())
for _ in range(150):
    emit(*mice())
for addressee, text, number in (("DL1ABC", "Hallo Welt", "1"), ("DA2JH-9", "Test 123", "42"), ("N0CALL", "no number", None)):
    body = text + (f"{{{number}" if number else "")
    emit(f":{addressee:<9}:{body}", "WIDE1-1", "APRS")
emit(f":{'DL1ABC':<9}:ack42", "WIDE1-1", "APRS")
emit(">Status text here", "WIDE1-1", "APRS")
emit("/092345z4903.50N/07201.75W>Test1234", "WIDE1-1", "APRS")
emit("@092345z4903.50N/07201.75W>Test1234", "WIDE1-1", "APRS")
emit("=4903.50N/07201.75W-Test /A=001234", "WIDE1-1", "APRS")
emit("=4903.50N/07201.75W_220/004g005t077r000p000P000h50b09900wRSW", "WIDE1-1", "APRS")
