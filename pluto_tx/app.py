#!/usr/bin/env python3
"""CLI entry point for the PlutoSDR TX app (stages 1-2: headless FM PoC + PTT).

Usage:
    python3 -m pluto_tx.app --freq 432150000 --duration 3
    python3 -m pluto_tx.app --freq 432150000 --interactive
"""
import argparse
import signal
import sys
import time

from . import config
from .flowgraph import PlutoTxFlowgraph


def build_argparser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--uri", default=config.DEFAULT_URI)
    p.add_argument("--freq", type=float, default=config.DEFAULT_FREQUENCY, help="Hz")
    p.add_argument("--atten", type=float, default=config.DEFAULT_ATTEN_CEILING,
                    help="TX attenuation in dB while keyed (default %(default)s)")
    p.add_argument("--mode", choices=["fm", "ssb"], default="fm")
    p.add_argument("--duration", type=float, default=3.0,
                    help="seconds to key up for in the non-interactive (default) test")
    p.add_argument("--interactive", action="store_true",
                    help="Enter to key/unkey repeatedly instead of a fixed-duration test")
    p.add_argument("--gui", action="store_true", help="launch the PyQt5 GUI instead of the CLI test")
    p.add_argument("--yes", action="store_true", help="skip the confirmation prompt (CLI mode only)")
    return p


def main(argv=None):
    args = build_argparser().parse_args(argv)

    if not (config.MIN_ATTEN <= args.atten <= config.MAX_ATTEN):
        print(f"--atten must be between {config.MIN_ATTEN} and {config.MAX_ATTEN}", file=sys.stderr)
        return 1

    band = config.in_amateur_band(args.freq)
    print(f"Frequency:   {args.freq/1e6:.4f} MHz" + (f" ({band})" if band else " (WARNING: outside known DE amateur bands)"))
    print(f"Mode:        {args.mode.upper()}")
    print(f"Attenuation: {args.atten:.2f} dB")
    print(f"Context:     {args.uri}")

    if args.gui:
        from .gui import run_gui
        mode = PlutoTxFlowgraph.MODE_SSB if args.mode == "ssb" else PlutoTxFlowgraph.MODE_FM

        def build_tb():
            return PlutoTxFlowgraph(uri=args.uri, frequency=args.freq, atten_ceiling_db=args.atten,
                                     mode=mode, enable_waterfall=True)

        return run_gui(build_tb) or 0

    if not args.yes:
        reply = input("Type YES to key up: ")
        if reply.strip() != "YES":
            print("Aborted.")
            return 1

    mode = PlutoTxFlowgraph.MODE_SSB if args.mode == "ssb" else PlutoTxFlowgraph.MODE_FM
    tb = PlutoTxFlowgraph(uri=args.uri, frequency=args.freq, atten_ceiling_db=args.atten, mode=mode)

    def sig_handler(signum, frame):
        print(f"\nSignal {signum} received, shutting down safely...")
        tb.shutdown_safe()
        sys.exit(0)

    signal.signal(signal.SIGINT, sig_handler)
    signal.signal(signal.SIGTERM, sig_handler)

    orig_excepthook = sys.excepthook

    def excepthook(exc_type, exc_value, exc_tb):
        print("Uncaught exception, forcing safe state...", file=sys.stderr)
        tb.shutdown_safe()
        orig_excepthook(exc_type, exc_value, exc_tb)

    sys.excepthook = excepthook

    try:
        tb.start()
        if args.interactive:
            print("Press Enter to key up, Enter again to unkey. Ctrl-C to quit.")
            while True:
                input()
                if not tb.keyed:
                    tb.key_ptt()
                    print("ON AIR")
                else:
                    tb.unkey_ptt()
                    print("unkeyed")
        else:
            print("Keying up...")
            tb.key_ptt()
            time.sleep(args.duration)
            tb.unkey_ptt()
            print("Done.")
    finally:
        tb.shutdown_safe()

    return 0


if __name__ == "__main__":
    sys.exit(main())
