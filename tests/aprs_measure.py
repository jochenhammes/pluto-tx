"""Measures the APRS demodulator against multimon-ng on the same audio, without radio or GNU Radio. Run:
python3 -m tests.aprs_measure"""
import subprocess
import sys
import time

import numpy as np

from pluto_advanced_rx.aprs_demod import AprsFrameDetector
from pluto_tx import aprs, aprs_codec as c

RATE = 22050  # what multimon-ng takes


def tilt(x, db, rate=RATE):
    """Space louder than Mark by `db`, as de-emphasis or missing pre-emphasis make it: a first-order slope in dB per octave."""
    if db == 0:
        return x
    X = np.fft.rfft(x)
    f = np.fft.rfftfreq(len(x), 1 / rate)
    gain = 10 ** (db / 20 * np.log2(np.maximum(f, 50) / 1600.0) / np.log2(2200 / 1200))
    return np.fft.irfft(X * gain, len(x)).astype(np.float32)


def scene(n, rng, snr_db, tilt_db=0.0, clock_error=0.0, preamble_s=0.3, gap_s=0.3):
    parts, sent = [], []
    rate = RATE * (1 + clock_error)
    for i in range(n):
        source = f"DA{i % 10}XY-{i % 16}"
        info = f"!{5000 + i % 900:04d}.{i % 100:02d}N/00900.{(i * 7) % 100:02d}E>packet {i} {rng.integers(0, 1 << 30)}".encode()
        frame = c.build_frame(source, "APRS", ["WIDE1-1"], info)
        parts += [np.zeros(int(gap_s * RATE), np.float32), aprs.encode_frame(frame, rate, preamble_s=preamble_s)]
        sent.append(info)
    x = np.concatenate(parts + [np.zeros(int(gap_s * RATE), np.float32)])
    x = tilt(x, tilt_db)
    power = np.mean(aprs.AMPLITUDE ** 2 / 2)
    noise = np.sqrt(power * 10 ** (-snr_db / 10))
    return x + noise * rng.standard_normal(len(x)).astype(np.float32), sent


def ours(x, sent, rate=RATE):
    got = []
    det = AprsFrameDetector(rate, got.append)
    for i in range(0, len(x), 4096):
        det.feed(x[i:i + 4096])
    infos = {g["info"] for g in got}
    return len(infos & set(sent)), len(infos - set(sent)), det


def multimon(x, sent):
    pcm = (np.clip(x * 0.4, -1, 1) * 32767).astype("<i2").tobytes()
    out = subprocess.run(["multimon-ng", "-q", "-t", "raw", "-a", "AFSK1200", "-"], input=pcm, capture_output=True, timeout=120).stdout
    text = out.decode("latin-1")
    return sum(1 for info in sent if info.decode() in text)


def compare(label, **kw):
    rng = np.random.default_rng(1)
    x, sent = scene(100, rng, **kw)
    a, wrong, _ = ours(x, sent)
    b = multimon(x, sent)
    print(f"  {label:34s} wir {a:3d}/100  multimon-ng {b:3d}/100" + (f"  falsch {wrong}" if wrong else ""))
    return a, b


def main():
    print("SNR im Audio (Weißrauschen), gleiche Daten:")
    for snr in (0, 2, 4, 6, 8, 10, 15, 20):
        compare(f"{snr} dB", snr_db=snr)
    print("Pegelunterschied Mark/Space bei 10 dB SNR:")
    for t in (-12, -6, -3, 3, 6, 12):
        compare(f"{t:+d} dB", snr_db=10, tilt_db=t)
    print("Takt des Senders bei 15 dB:")
    for e in (-0.02, -0.01, 0.01, 0.02):
        compare(f"{e:+.0%}", snr_db=15, clock_error=e)
    print("Kurze Präambel bei 15 dB:")
    for p in (0.1, 0.05, 0.02):
        compare(f"{p * 1000:.0f} ms", snr_db=15, preamble_s=p)
    rng = np.random.default_rng(2)
    n = RATE * 120
    noise = rng.standard_normal(n).astype(np.float32)
    got = []
    det = AprsFrameDetector(RATE, got.append)
    t0 = time.perf_counter()
    for i in range(0, n, 4096):
        det.feed(noise[i:i + 4096])
    el = time.perf_counter() - t0
    print(f"Rauschen 120 s: {len(got)} falsche Pakete, {det.fcs_failures} FCS-Fehler, {el / 120:.1%} eines Kerns bei {RATE} S/s")
    rng = np.random.default_rng(3)
    x, _ = scene(30, rng, snr_db=20)
    for rate, label in ((12500, "12,5 kS/s"), (13200, "13,2 kS/s"), (22050, "22,05 kS/s"), (48000, "48 kS/s")):
        y = np.interp(np.arange(int(len(x) * rate / RATE)) * RATE / rate, np.arange(len(x)), x).astype(np.float32)
        print(f"Abtastrate {label}:", ours(y, _, rate)[0], "/ 30")


if __name__ == "__main__":
    sys.exit(main())
