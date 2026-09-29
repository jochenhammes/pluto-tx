#!/usr/bin/env python3
"""JS8 off-air reference recordings (J0 of docs/JS8_PLAN.md) -- receive only, never transmits.

    record  continuous 12 kHz USB audio from an RX device, via the same AdvancedRxFlowgraph path the
            FT8 receiver uses (IF -> 12 kHz -> 200..3000 Hz band filter -> real), into
            OUT/audio.f32 (raw float32) plus OUT/timing.json (sample index <-> wall clock pairs)
    cut     UTC-aligned slot WAVs (12 kHz, 16 bit, peak-normalised like the FT8 decoder) for one or
            more JS8 speeds: OUT/slots/<speed>/<YYYYmmdd_HHMMSS>.wav

Examples:
    tools/js8_record_slots.py record --device hackrf --freq 7078000 --minutes 30 --out ~/js8-ref/offair/40m
    tools/js8_record_slots.py record --device rtlsdr --direct-sampling q --freq 14078000 --minutes 30 --out ...
    tools/js8_record_slots.py cut ~/js8-ref/offair/40m --speeds normal,fast,turbo,slow
"""
import argparse
import json
import os
import sys
import threading
import time
import wave

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

SAMPLE_RATE = 12000
# JS8_Include/commons.h (JS8Call v2.5.2): period in seconds per speed
SPEEDS = {"normal": 15, "fast": 10, "turbo": 6, "slow": 30}
TIMING_EVERY_S = 1.0


def _make_recorder_class(out_dir):
    from gnuradio import gr

    class Js8Recorder(gr.sync_block):
        """Drop-in for Ft8Receiver (same constructor): appends every sample to audio.f32 and notes
        (total samples written, wall time of the newest sample) about once a second."""

        def __init__(self, on_decodes=None, backend=None, clock=time.time, my_call="", my_grid=""):
            gr.sync_block.__init__(self, name="Js8Recorder", in_sig=[np.float32], out_sig=None)
            self._clock = clock
            self._f = open(os.path.join(out_dir, "audio.f32"), "wb")
            self._lock = threading.Lock()
            self.written = 0
            self.timing = []
            self._last_note = 0.0

        def work(self, input_items, output_items):
            x = input_items[0]
            t = self._clock()
            with self._lock:
                self._f.write(np.asarray(x, dtype=np.float32).tobytes())
                self.written += len(x)
                if t - self._last_note >= TIMING_EVERY_S:
                    self.timing.append([self.written, t])
                    self._last_note = t
            return len(x)

        def start(self):
            return True

        def stop(self):
            return True

        def close(self):
            with self._lock:
                self._f.close()

    return Js8Recorder


def cmd_record(args):
    from pluto_advanced_rx import devices as rx_devices
    from pluto_advanced_rx import ft8_rx
    from pluto_advanced_rx.flowgraph import AdvancedRxFlowgraph

    out = os.path.expanduser(args.out)
    os.makedirs(out, exist_ok=True)
    recorder_cls = _make_recorder_class(out)
    ft8_rx.Ft8Receiver = recorder_cls          # the flowgraph imports it at build time

    device_cls = rx_devices.DEVICE_REGISTRY[args.device]
    uri = args.uri or device_cls.DEFAULT_CONNECTION
    direct = {"off": 0, "i": 1, "q": 2}[args.direct_sampling]
    tb = AdvancedRxFlowgraph(
        device_type=args.device, uri=uri, frequency=args.freq, sample_rate=args.bandwidth,
        gain_mode=args.gain_mode, manual_gain_db=args.gain, demod_mode=AdvancedRxFlowgraph.MODE_SSB,
        audio_device="", active_digimode="ft8", direct_sampling=direct)
    rec = tb.ft8_receiver
    meta = dict(device=args.device, uri=uri, dial_hz=args.freq, direct_sampling=args.direct_sampling,
                gain_mode=args.gain_mode, gain_db=args.gain, sample_rate=SAMPLE_RATE,
                started_utc=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
    tb.start()
    try:
        tb.set_rx_muted(True)
    except Exception:
        pass
    t_end = time.time() + args.minutes * 60
    print(f"recording {args.freq / 1e6:.3f} MHz USB for {args.minutes} min -> {out}", flush=True)
    try:
        while time.time() < t_end:
            time.sleep(5)
            print(f"  {rec.written / SAMPLE_RATE:7.1f} s recorded", flush=True)
    except KeyboardInterrupt:
        pass
    finally:
        tb.stop()
        tb.wait()
        rec.close()
        meta["stopped_utc"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        meta["samples"] = rec.written
        meta["timing"] = rec.timing
        with open(os.path.join(out, "timing.json"), "w") as f:
            json.dump(meta, f, indent=1)
    print(f"done: {rec.written / SAMPLE_RATE:.1f} s", flush=True)
    return 0


def sample_time_fit(timing):
    """Least-squares fit wall_time = t0 + idx / rate_eff over the (index, time) notes; returns
    (t0 of sample 0, effective sample rate)."""
    a = np.asarray(timing, dtype=np.float64)
    idx, t = a[:, 0] - 1, a[:, 1]
    slope, t0 = np.polyfit(idx, t, 1)
    return t0, 1.0 / slope


def write_wav(path, x):
    x = x - float(np.mean(x)) if len(x) else x     # the HackRF's DC spike leaks through as an offset
    peak = float(np.max(np.abs(x))) if len(x) else 0.0
    y = x / peak if peak > 0 else x
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        w.writeframes((y * 20000).astype("<i2").tobytes())


def cmd_cut(args):
    src = os.path.expanduser(args.dir)
    with open(os.path.join(src, "timing.json")) as f:
        meta = json.load(f)
    audio = np.fromfile(os.path.join(src, "audio.f32"), dtype=np.float32)
    t0, rate = sample_time_fit(meta["timing"])
    t0 -= args.latency_s
    print(f"{len(audio) / SAMPLE_RATE:.1f} s audio, sample 0 at {t0:.3f}, effective rate {rate:.2f} Hz")
    for speed in args.speeds.split(","):
        period = SPEEDS[speed]
        d = os.path.join(src, "slots", speed)
        os.makedirs(d, exist_ok=True)
        t_last = t0 + len(audio) / rate
        slot = np.ceil(t0 / period) * period
        n = 0
        while slot + period <= t_last:
            i0 = int(round((slot - t0) * rate))
            x = audio[i0:i0 + period * SAMPLE_RATE]
            name = time.strftime("%Y%m%d_%H%M%S", time.gmtime(slot)) + ".wav"
            write_wav(os.path.join(d, name), x)
            slot += period
            n += 1
        print(f"  {speed}: {n} slots -> {d}")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("record")
    r.add_argument("--device", default="hackrf", choices=["hackrf", "rtlsdr", "pluto"])
    r.add_argument("--uri", default=None)
    r.add_argument("--freq", type=float, required=True, help="USB dial frequency in Hz")
    r.add_argument("--minutes", type=float, default=30.0)
    r.add_argument("--direct-sampling", default="off", choices=["off", "i", "q"])
    r.add_argument("--bandwidth", type=int, default=None)
    r.add_argument("--gain-mode", default="slow_attack")
    r.add_argument("--gain", type=float, default=40.0)
    r.add_argument("--out", required=True)
    r.set_defaults(func=cmd_record)
    c = sub.add_parser("cut")
    c.add_argument("dir")
    c.add_argument("--speeds", default="normal,fast,turbo,slow")
    c.add_argument("--latency-s", type=float, default=0.0,
                   help="device/flowgraph latency subtracted from the wall-clock stamps")
    c.set_defaults(func=cmd_cut)
    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
