#!/usr/bin/env python3
"""Send an unmodulated carrier from the PlutoSDR TX port for a fixed duration.

Requires libiio-utils (iio_attr, iio_writedev) on PATH.

Example:
    ./pluto_tx_carrier.py --freq 432150000 --duration 3 --atten -30
"""
import argparse
import struct
import subprocess
import sys
import tempfile
import os

DEVICE_PHY = "ad9361-phy"
DEVICE_TX = "cf-ad9361-dds-core-lpc"
MIN_ATTEN = -89.75  # max attenuation == min output power


def iio_attr(uri, *args):
    cmd = ["iio_attr", "-u", uri, "-q", *args]
    subprocess.run(cmd, check=True)


def set_tx_atten(uri, atten_db):
    iio_attr(uri, "-o", "-c", DEVICE_PHY, "voltage0", "hardwaregain", str(atten_db))


def set_tx_lo_powerdown(uri, powered_down: bool):
    iio_attr(uri, "-c", DEVICE_PHY, "altvoltage1", "powerdown", "1" if powered_down else "0")


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--freq", type=float, required=True, help="carrier frequency in Hz")
    p.add_argument("--duration", type=float, default=3.0, help="transmit duration in seconds (default 3)")
    p.add_argument("--atten", type=float, default=-30.0, help="TX attenuation in dB, 0..-89.75 (default -30, low power)")
    p.add_argument("--samplerate", type=float, default=2_500_000, help="TX sample rate in Hz (default 2.5 MSps, min ~2.083 MHz)")
    p.add_argument("--bandwidth", type=float, default=1_000_000, help="TX analog filter bandwidth in Hz (default 1 MHz)")
    p.add_argument("--amplitude", type=int, default=5000, help="carrier amplitude, int16 counts (default 5000)")
    p.add_argument("--uri", default="ip:plutoplus.local", help="libiio context URI (default ip:plutoplus.local)")
    p.add_argument("--yes", action="store_true", help="skip the interactive confirmation prompt")
    args = p.parse_args()

    if not (MIN_ATTEN <= args.atten <= 0):
        p.error(f"--atten must be between {MIN_ATTEN} and 0")

    approx_dbm = 0 + args.atten  # Pluto TX ballpark: ~0 dBm at 0 dB attenuation
    approx_mw = 10 ** (approx_dbm / 10)
    print(f"Target:      {args.freq/1e6:.4f} MHz")
    print(f"Duration:    {args.duration:.2f} s")
    print(f"Attenuation: {args.atten:.2f} dB  (approx {approx_dbm:.1f} dBm / {approx_mw:.3f} mW)")
    print(f"Context:     {args.uri}")
    print("Make sure you are licensed for this frequency/power and identify per your license conditions.")

    if not args.yes:
        reply = input("Type YES to key up: ")
        if reply.strip() != "YES":
            print("Aborted.")
            sys.exit(1)

    n_samples = max(1, int(args.duration * args.samplerate))
    sample = struct.pack("<hh", args.amplitude, 0)  # constant I, Q=0 -> CW tone at LO

    tmp = tempfile.NamedTemporaryFile(prefix="pluto_carrier_", suffix=".raw", delete=False)
    try:
        tmp.write(sample * n_samples)
        tmp.close()

        # Safety first: max attenuation before touching frequency/LO power.
        set_tx_atten(args.uri, MIN_ATTEN)
        iio_attr(args.uri, "-c", DEVICE_PHY, "altvoltage1", "frequency", str(int(args.freq)))
        iio_attr(args.uri, "-o", "-c", DEVICE_PHY, "voltage0", "sampling_frequency", str(int(args.samplerate)))
        iio_attr(args.uri, "-o", "-c", DEVICE_PHY, "voltage0", "rf_bandwidth", str(int(args.bandwidth)))
        set_tx_lo_powerdown(args.uri, False)

        # Now ramp up to the requested power and stream the carrier.
        set_tx_atten(args.uri, args.atten)
        print("Transmitting...")
        with open(tmp.name, "rb") as f:
            subprocess.run(
                ["iio_writedev", "-u", args.uri, "-b", "32768", DEVICE_TX, "voltage0", "voltage1"],
                stdin=f,
                check=True,
            )
        print("Done.")
    finally:
        try:
            set_tx_atten(args.uri, MIN_ATTEN)
            set_tx_lo_powerdown(args.uri, True)
        except subprocess.CalledProcessError as e:
            print(f"WARNING: failed to safe the TX chain: {e}", file=sys.stderr)
        os.unlink(tmp.name)


if __name__ == "__main__":
    main()
