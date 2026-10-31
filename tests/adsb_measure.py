"""Measures the ADS-B demodulator without radio or GNU Radio: reception over SNR, sample rate, overlaps, a DC spike,
false messages on noise and CPU under load. Run: python3 -m tests.adsb_measure"""
import sys
import time

import numpy as np

from pluto_advanced_rx.adsb_demod import AdsbFrameDetector
from tests import adsb_testgen as gen

BURST_S = 120e-6


def channel(x, rng, snr_db):
    """SNR of the pulse against the noise power in the whole sampled bandwidth."""
    sigma = np.sqrt(10 ** (-snr_db / 10) / 2)
    return x + sigma * (rng.standard_normal(len(x)) + 1j * rng.standard_normal(len(x)))


def run(iq, rate, chunk=None, prior_known=()):
    got = []
    det = AdsbFrameDetector(rate, got.append)
    for a in prior_known:
        det._known[a] = 0.0
    magnitude = np.abs(iq).astype(np.float32)
    if chunk:
        for i in range(0, len(magnitude), chunk):
            det.feed(magnitude[i:i + chunk])
    else:
        det.feed(magnitude)
    det.flush()
    return got, det


def stream(rate, n, rng, gap_us=300.0, snr_db=None, offset_hz=0.0):
    icaos = [int(rng.integers(1, 1 << 24)) for _ in range(n)]
    parts, spu = [], rate / 1e6
    for icao in icaos:
        parts.append(np.zeros(int(gap_us * spu + rng.random() * spu), np.complex128))
        m = gen.position_message(icao, float(rng.uniform(40, 60)), float(rng.uniform(0, 20)), 30000, int(rng.integers(0, 2)))
        parts.append(gen.iq(m, rate, phase_rad=float(rng.uniform(0, 6.28)), freq_offset_hz=offset_hz, delay_us=float(rng.random())))
    x = np.concatenate(parts + [np.zeros(int(200 * spu), np.complex128)])
    return (channel(x, rng, snr_db) if snr_db is not None else x), icaos


def quote(rate, snr, n=300, seed=1, **kw):
    iq, icaos = stream(rate, n, np.random.default_rng(seed), snr_db=snr, **kw)
    got, det = run(iq, rate)
    return len({int(m["icao"], 16) for m in got} & set(icaos)) / n, det, len({int(m["icao"], 16) for m in got} - set(icaos))


def main():
    print("Empfangsquote über SNR (Pulsleistung zum Rauschen in der ganzen Abtastbandbreite; Pulslage zufällig gegen den Abtasttakt)")
    rates = (2_000_000, 2_048_000, 2_400_000, 2_500_000, 3_200_000, 4_000_000, 5_000_000)
    print("  SNR   " + "".join(f"{r / 1e6:>8.1f}" for r in rates))
    for snr in (3, 6, 8, 10, 12, 15, 20, 30):
        print(f"  {snr:3d} dB" + "".join(f"{quote(r, snr)[0]:8.0%}" for r in rates))
    print("Frequenzablage bis 500 kHz, 15 dB (Betrag: sollte egal sein):", [f"{quote(2_400_000, 15, offset_hz=o)[0]:.0%}" for o in (0, 100e3, 300e3, 500e3)])
    rng = np.random.default_rng(3)
    rate = 2_400_000
    print("DC-Spike (konstanter Träger relativ zum Puls, 20 dB SNR, mit und ohne Mittelwertabzug):")
    for level_db in (-10, -3, 0, 6, 10):
        iq, icaos = stream(rate, 200, np.random.default_rng(4), snr_db=20)
        spike = 10 ** (level_db / 20)
        a = run(iq + spike, rate)[0]
        cleaned = iq + spike
        cleaned = cleaned - np.mean(cleaned)
        b = run(cleaned, rate)[0]
        print(f"  {level_db:+3d} dB: ohne {len({int(m['icao'], 16) for m in a} & set(icaos)) / 200:5.0%}  mit {len({int(m['icao'], 16) for m in b} & set(icaos)) / 200:5.0%}")
    print("Überlagerung: zweiter Rahmen um 40 us versetzt, relativ zum ersten:")
    for level_db in (-20, -10, -6, 0):
        a, b = 0, 0
        for k in range(100):
            m1 = gen.position_message(0x111111, 50.0, 8.0, 30000, 0)
            m2 = gen.position_message(0x222222, 51.0, 9.0, 30000, 0)
            x1, x2 = gen.iq(m1, rate), gen.iq(m2, rate, phase_rad=float(rng.uniform(0, 6.28)), amplitude=10 ** (level_db / 20))
            x = np.zeros(int(300 * rate / 1e6), np.complex128)
            x[20:20 + len(x1)] += x1
            o = 20 + int(40 * rate / 1e6)
            x[o:o + len(x2)] += x2
            got = run(channel(x, rng, 25), rate)[0]
            a += any(m["icao"] == "111111" for m in got)
            b += any(m["icao"] == "222222" for m in got)
        print(f"  {level_db:+4d} dB: erster {a:3d}/100, zweiter {b:3d}/100")
    print("Rauschen:")
    for rate in (2_400_000, 4_000_000):
        rng = np.random.default_rng(7)
        n = rate * 60
        noise = np.sqrt(rng.standard_normal(n).astype(np.float32) ** 2 + rng.standard_normal(n).astype(np.float32) ** 2)
        got, det = [], None
        det = AdsbFrameDetector(rate, got.append)
        t0 = time.perf_counter()
        for i in range(0, len(noise), 65536):
            det.feed(noise[i:i + 65536])
        det.flush()
        el = time.perf_counter() - t0
        print(f"  {rate / 1e6:.1f} MS/s, {n / rate:.0f} s: {det.candidates} Kandidaten, {len(got)} falsche Nachrichten, {el / (n / rate):.1%} eines Kerns")
    print("Dauerlast 500 Nachrichten/s, 15 dB:")
    for rate in (2_400_000, 4_000_000):
        rng = np.random.default_rng(9)
        iq, icaos = stream(rate, 5000, rng, gap_us=1880.0, snr_db=15)
        magnitude = np.abs(iq).astype(np.float32)
        got = []
        det = AdsbFrameDetector(rate, got.append)
        t0 = time.perf_counter()
        for i in range(0, len(magnitude), 65536):
            det.feed(magnitude[i:i + 65536])
        det.flush()
        el = time.perf_counter() - t0
        secs = len(magnitude) / rate
        print(f"  {rate / 1e6:.1f} MS/s: {secs:.1f} s Signal, {len(got)} Nachrichten erkannt von {len(icaos)}, {el / secs:.1%} eines Kerns, Korrigiert {det.corrected}")


if __name__ == "__main__":
    sys.exit(main())
