#!/usr/bin/env python3
"""Reference WAVs from js8ref vectors: port of JS8Call Modulator::readData
(JS8_Mode/Modulator.cpp, v2.5.2) at 48 kHz, decimated to 12 kHz/16 bit.

usage: mkwav.py vectors.jsonl OUTDIR [--f0 1500] [--snr DB] [--cases a,b]
One WAV per frame, slot-aligned (sample 0 = slot start), silence before
startDelayMS as in the modulator."""
import argparse
import json
import math
import os
import wave

import numpy as np
from scipy.signal import resample_poly

FRAME_RATE = 48000
# JS8_Include/commons.h: symbol samples @12 kHz, period, start delay
SUBMODES = {0: (1920, 15, 500), 1: (1200, 10, 200), 2: (600, 6, 100), 4: (3840, 30, 500)}


def modulate(tones, submode, f0):
    nsps, period, start_ms = SUBMODES[submode]
    spacing = 12000.0 / nsps
    i0 = int((79 - 0.017) * 4.0 * nsps)
    i1 = int(79 * 4.0 * nsps)
    out = np.zeros(period * FRAME_RATE, dtype=np.float64)
    pos = start_ms * FRAME_RATE // 1000
    phi = 0.0
    amp = 32767.0
    isym0 = -1
    dphi = 0.0
    tau = 2 * math.pi
    for ic in range(i1):
        isym = int(ic / (4.0 * nsps))
        if isym != isym0:
            dphi = tau * (f0 + tones[isym] * spacing) / FRAME_RATE
            isym0 = isym
        phi += dphi
        if phi > tau:
            phi -= tau
        if ic > i0:
            amp *= 0.98
        if pos + ic < out.size:
            out[pos + ic] = round(amp * math.sin(phi))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("vectors")
    ap.add_argument("outdir")
    ap.add_argument("--f0", type=float, default=1500.0)
    ap.add_argument("--snr", type=float, default=None, help="SNR in 2500 Hz (dB)")
    ap.add_argument("--cases", default="")
    ap.add_argument("--seed", type=int, default=1)
    a = ap.parse_args()
    want = set(filter(None, a.cases.split(",")))
    os.makedirs(a.outdir, exist_ok=True)
    rng = np.random.default_rng(a.seed)
    for line in open(a.vectors):
        o = json.loads(line)
        if want and o["case"] not in want:
            continue
        for i, fr in enumerate(o["frames"]):
            tones = [int(c) for c in fr["tones"]]
            x48 = modulate(tones, o["submode"], a.f0)
            x = resample_poly(x48, 1, 4)
            if a.snr is not None:
                x = x / 32767.0 * 0.1  # headroom
                sig_p = np.mean(x[np.abs(x) > 0] ** 2)
                # noise power in 2500 Hz of a 6000 Hz wide real signal
                n_p = sig_p / (10 ** (a.snr / 10)) * (6000 / 2500)
                x = x + rng.normal(0, math.sqrt(n_p), x.size)
                x = x * 32767.0
            x = np.clip(np.round(x), -32768, 32767).astype("<i2")
            name = f"{o['case']}_f{i}.wav"
            with wave.open(os.path.join(a.outdir, name), "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(12000)
                w.writeframes(x.tobytes())


if __name__ == "__main__":
    main()
