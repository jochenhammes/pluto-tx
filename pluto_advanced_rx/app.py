#!/usr/bin/env python3
"""CLI entry point for the PlutoSDR advanced RX app: FM/SSB(USB) demodulation
plus an interactive, SDR++-style waterfall (click-to-tune, tuned-frequency
marker, demod-bandwidth shading).

Usage:
    python3 -m pluto_advanced_rx.app --freq 432150000 --mode fm
"""
import argparse
import sys

from . import config
from .flowgraph import AdvancedRxFlowgraph


def build_argparser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--uri", default=config.DEFAULT_URI)
    p.add_argument("--freq", type=float, default=config.DEFAULT_FREQUENCY, help="Hz")
    p.add_argument("--mode", choices=["fm", "ssb"], default="fm")
    p.add_argument("--bandwidth", type=int, choices=config.RX_BANDWIDTH_PRESETS,
                    default=config.DEFAULT_RX_BANDWIDTH, help="RX sample rate / zoom span, Hz")
    p.add_argument("--gain-mode", choices=config.GAIN_MODES, default=config.DEFAULT_GAIN_MODE)
    p.add_argument("--gain", type=float, default=config.DEFAULT_MANUAL_GAIN_DB,
                    help="manual RX gain in dB (only used when --gain-mode manual)")
    return p


def main(argv=None):
    args = build_argparser().parse_args(argv)

    band = config.in_amateur_band(args.freq)
    print(f"Frequency:  {args.freq/1e6:.4f} MHz" + (f" ({band})" if band else " (outside known DE amateur bands)"))
    print(f"Mode:       {args.mode.upper()}")
    print(f"Bandwidth:  {args.bandwidth/1e6:g} MHz")
    print(f"Gain mode:  {args.gain_mode}" + (f", manual gain: {args.gain} dB" if args.gain_mode == "manual" else ""))
    print(f"Context:    {args.uri}")

    from .gui import run_gui

    demod_mode = AdvancedRxFlowgraph.MODE_SSB if args.mode == "ssb" else AdvancedRxFlowgraph.MODE_FM

    def build_tb():
        return AdvancedRxFlowgraph(uri=args.uri, frequency=args.freq, sample_rate=args.bandwidth,
                                    gain_mode=args.gain_mode, manual_gain_db=args.gain,
                                    demod_mode=demod_mode)

    return run_gui(build_tb) or 0


if __name__ == "__main__":
    sys.exit(main())
