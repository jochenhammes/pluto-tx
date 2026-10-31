"""Measures the ADS-L burst demodulator without radio or GNU Radio: packet error rate over SNR, frequency offset,
device sample rate, false packets on noise and CPU load. Run: python3 -m tests.adsl_measure"""
import sys
import time

import numpy as np

from pluto_advanced_rx.adsl_demod import (AdslBurstDetector, CHANNEL_CUTOFF_HZ, CHANNEL_TRANS_HZ as CHANNEL_TRANSITION_HZ,
                                          DEVIATION_HZ, WORKING_RATE_HZ)
from tests import adsl_testgen as gen

NOISE_BW_HZ = 200_000  # the SNR below is referred to the 200 kHz channel of the specification


def lowpass(rate):
    n = int(3.3 * rate / CHANNEL_TRANSITION_HZ) | 1
    t = np.arange(n) - n // 2
    h = np.sinc(2 * CHANNEL_CUTOFF_HZ / rate * t) * np.hamming(n)
    return h / h.sum()


def decimation(rate):
    return max(1, round(rate / WORKING_RATE_HZ))


def channelize(iq, rate):
    decim = decimation(rate)
    filtered = np.convolve(iq, lowpass(rate), mode="same")[::decim]
    out_rate = rate / decim
    disc = np.angle(filtered[1:] * np.conj(filtered[:-1])) * out_rate / (2 * np.pi * DEVIATION_HZ)
    return disc.astype(np.float32), out_rate


def burst_iq(rate, rng, snr_db, offset_hz, n_packets, invert=False):
    """Random-content Traffic frames with noise. Returns IQ and the list of sent addresses."""
    gap = np.zeros(int(0.004 * rate), np.complex64)
    parts = [gap]
    sent = []
    for _ in range(n_packets):
        address = int(rng.integers(0, 1 << 24))
        clear = gen.traffic_clear(address=address, lat=float(rng.uniform(-80, 80)), lon=float(rng.uniform(-170, 170)),
                                  alt_m=int(rng.integers(0, 5000)), speed_mps=float(rng.uniform(0, 80)))
        sent.append(address)
        parts += [gen.modulate(gen.air_chips(gen.frame(clear)), rate, invert=invert), gap]
    x = np.concatenate(parts)
    x = x * np.exp(2j * np.pi * offset_hz * np.arange(len(x)) / rate)
    sigma = np.sqrt(10 ** (-snr_db / 10) * rate / NOISE_BW_HZ / 2)
    return x + sigma * (rng.standard_normal(len(x)) + 1j * rng.standard_normal(len(x))), sent


def received(rate, snr_db, offset_hz=0.0, n_packets=100, seed=1, invert=False):
    rng = np.random.default_rng(seed)
    iq, sent = burst_iq(rate, rng, snr_db, offset_hz, n_packets, invert)
    disc, out_rate = channelize(iq, rate)
    got = []
    detector = AdslBurstDetector(out_rate, got.append)
    detector.feed(disc)
    detector.flush()
    missing = set(sent) - {p["address"] for p in got}
    return 1 - len(missing) / len(sent), detector, got


def mixed(rate, wanted_snr_db, make_interferer, n_packets=100, seed=3, offset_hz=0.0):
    rng = np.random.default_rng(seed)
    iq, sent = burst_iq(rate, rng, wanted_snr_db, offset_hz, n_packets)
    iq = iq + make_interferer(len(iq), rng)
    disc, out_rate = channelize(iq, rate)
    got = []
    detector = AdslBurstDetector(out_rate, got.append)
    detector.feed(disc)
    detector.flush()
    wanted = len(set(sent) - {p["address"] for p in got})
    return 1 - wanted / len(sent), len({p["address"] for p in got} - set(sent)), detector


def tone(rate, freq_hz, level_db):
    return lambda n, rng: 10 ** (level_db / 20) * np.exp(2j * np.pi * freq_hz * np.arange(n) / rate)


def burst_train(rate, freq_hz, level_db, sync_chips, frame_chips, period_s=0.012):
    """Other senders with the same modulation on `freq_hz`: legacy-style frames (random content, other sync)."""
    def make(n, rng):
        out = np.zeros(n, np.complex128)
        step = int(period_s * rate)
        for start in range(0, n - step, step):
            chips = sync_chips + [int(v) for v in rng.integers(0, 2, frame_chips)]
            b = gen.modulate(chips, rate)
            out[start:start + len(b)] += b
        return 10 ** (level_db / 20) * out * np.exp(2j * np.pi * freq_hz * np.arange(n) / rate)
    return make


def interference(rate):
    print("Störer (gewünschtes Signal 20 dB SNR, je 100 Pakete; 'falsch' = Pakete anderer Adresse):")
    for label, item in (
        ("Träger im Kanal, +10 dB", tone(rate, 20_000, 10)),
        ("Träger im Kanal, 0 dB", tone(rate, 20_000, 0)),
        ("Träger im Kanal, -10 dB", tone(rate, 20_000, -10)),
        ("Träger bei 120 kHz, 0 dB", tone(rate, 120_000, 0)),
    ):
        ok, wrong, d = mixed(rate, 20, item)
        print(f"  {label:32s} {ok:6.1%}  falsch {wrong}  Kandidaten {d.candidates}")
    flarm_sync = [0, 1] * 8 + gen.manchester_chips(bytes((0xF5, 0x31, 0xFA, 0xB6)))
    for label, item in (
        ("FLARM-artig im Kanal, -10 dB", burst_train(rate, 0, -10, flarm_sync, 416)),
        ("FLARM-artig im Kanal, +10 dB", burst_train(rate, 0, 10, flarm_sync, 416)),
        ("FLARM-artig 200 kHz weg, 0 dB", burst_train(rate, 200_000, 0, flarm_sync, 416)),
        ("FLARM-artig 200 kHz weg, +20 dB", burst_train(rate, 200_000, 20, flarm_sync, 416)),
        ("FLARM-artig 200 kHz weg, +40 dB", burst_train(rate, 200_000, 40, flarm_sync, 416)),
    ):
        ok, wrong, d = mixed(rate, 20, item)
        print(f"  {label:32s} {ok:6.1%}  falsch {wrong}  Kandidaten {d.candidates}")
    print("Nachbarkanal: ein starkes ADS-L-Paket auf dem anderen Kanal (200 kHz weg), SNR dort:")
    for snr in (20, 40, 60):
        rng = np.random.default_rng(5)
        iq, sent = burst_iq(rate, rng, snr, 200_000, 100)
        disc, out_rate = channelize(iq, rate)
        got = []
        d = AdslBurstDetector(out_rate, got.append)
        d.feed(disc)
        d.flush()
        print(f"  {snr:3d} dB: auf dem falschen Kanal dekodiert: {len({p['address'] for p in got} & set(sent))}/{len(sent)}")


def main():
    print("Empfangsquote über SNR (2,4 MS/s, kein Versatz, je 200 Pakete)")
    for snr in (6, 8, 10, 12, 14, 16, 20, 30):
        print(f"  {snr:3d} dB: {received(2_400_000, snr, n_packets=200)[0]:6.1%}")
    print("Empfangsquote über Frequenzversatz (14 und 20 dB)")
    for off in (-60, -50, -40, -20, 0, 20, 40, 50, 60):
        a = received(2_400_000, 14, off * 1e3)[0]
        b = received(2_400_000, 20, off * 1e3)[0]
        print(f"  {off:+4d} kHz: {a:6.1%} {b:6.1%}")
    print("Empfangsquote über Geräterate (14 und 20 dB)")
    for rate in (1_800_000, 1_920_000, 2_000_000, 2_048_000, 2_400_000, 2_500_000, 3_200_000, 4_000_000, 5_000_000):
        decim = decimation(rate)
        print(f"  {rate / 1e6:5.3f} MS/s (Dezimierung {decim}, {rate / decim / 1e3:6.1f} kS/s): "
              f"{received(rate, 14)[0]:6.1%} {received(rate, 20)[0]:6.1%}")
    print(f"Invertiert, 20 dB: {received(2_400_000, 20, invert=True)[0]:.1%}")
    rng = np.random.default_rng(7)
    rate = 2_400_000
    n = rate * 40
    print(f"Bandbegrenztes Rauschen, {n / rate:.0f} s:")
    noise = rng.standard_normal(n) + 1j * rng.standard_normal(n)
    disc, out_rate = channelize(noise, rate)
    got = []
    detector = AdslBurstDetector(out_rate, got.append)
    t0 = time.perf_counter()
    for i in range(0, len(disc), 8192):
        detector.feed(disc[i:i + 8192])
    elapsed = time.perf_counter() - t0
    seconds = len(disc) / out_rate
    print(f"  {detector.candidates} Kandidaten, {detector.frames_bad} CRC-Fehler, {len(got)} falsche Pakete")
    print(f"  CPU je Kanal: {elapsed / seconds:.2%} eines Kerns")
    print("Schwelle gegen Rauschen und Empfang:")
    for threshold in (0.3, 0.35, 0.4, 0.45, 0.5, 0.6):
        d = AdslBurstDetector(out_rate, lambda p: None, threshold=threshold)
        d.feed(disc[:len(disc) // 4])
        rates = []
        for snr in (8, 10):
            iq, sent = burst_iq(rate, np.random.default_rng(1), snr, 0, 200)
            dd, r = channelize(iq, rate)
            g = []
            dt = AdslBurstDetector(r, g.append, threshold=threshold)
            dt.feed(dd)
            dt.flush()
            rates.append(1 - len(set(sent) - {p["address"] for p in g}) / len(sent))
        print(f"  {threshold:.2f}: {d.candidates / (seconds / 4):8.1f} Kandidaten/s auf Rauschen, 8 dB {rates[0]:6.1%}, 10 dB {rates[1]:6.1%}")
    interference(rate)
    rng = np.random.default_rng(8)
    iq, sent = burst_iq(400_000, rng, 25, 0, 500)
    disc, _ = channelize(iq, 400_000)
    got = []
    detector = AdslBurstDetector(400_000, got.append)
    t0 = time.perf_counter()
    for i in range(0, len(disc), 8192):
        detector.feed(disc[i:i + 8192])
    detector.flush()
    elapsed = time.perf_counter() - t0
    print(f"  Dauerlast {len(sent)} Pakete in {len(disc) / 400_000:.1f} s: {elapsed / (len(disc) / 400_000):.2%} eines Kerns,"
          f" erkannt {len(got)}")


if __name__ == "__main__":
    sys.exit(main())
