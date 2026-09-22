#!/usr/bin/env python3
"""
tx-stdin.py -- Transmit audio from stdin over a Quansheng UV-K5 via an
AIOC (All-In-One-Cable) adapter.

WHAT IT DOES
    Reads audio from stdin, keys the radio's PTT, streams the audio to
    the AIOC's ALSA playback device, and releases PTT automatically as
    soon as stdin reaches EOF (i.e. the audio source stops/ends). PTT is
    ALWAYS released on exit -- normal end, error, Ctrl-C, or the safety
    timeout below -- so the transmitter can never get stuck keyed up.

    The PTT sequencing (serial DTR/RTS scheme, lead-in/tail-out timing,
    idempotent release) lives in aioc_ptt.py -- shared with this app's own
    AIOC TX device backend (pluto_tx/devices/aioc.py), so both stay in
    lockstep. See that module's docstring for the DTR/RTS hardware detail.

USAGE
    # A WAV file/stream: header carries sample rate/format, no extra flags
    cat message.wav | python3 tx-stdin.py --wav
    some_tts_tool --format wav | python3 tx-stdin.py --wav

    # Raw PCM (no header) -- must tell aplay the format explicitly
    some_tts_tool --format raw --rate 22050 | \\
        python3 tx-stdin.py --rate 22050 --channels 1 --format S16_LE

    # Custom serial/ALSA device names (defaults match a standard AIOC setup)
    python3 tx-stdin.py --wav --port /dev/ttyACM0 \\
        --device plughw:AllInOneCable,0 < message.wav

SAFETY
    - PTT release happens in a `finally` block and in the SIGINT/SIGTERM
      handler, so it runs on every exit path (normal, exception, signal).
    - --max-seconds is a hard cutoff that kills playback and releases PTT
      if the audio source never closes stdin (e.g. a hung pipe), so a
      broken caller cannot leave the transmitter keyed up indefinitely.

LEGAL
    Only transmit on frequencies/modes/power levels you are licensed for,
    and identify with your callsign per your country's amateur radio
    regulations. This script does not set the radio's frequency -- tune
    the radio itself before running it.
"""

import argparse
import signal
import subprocess
import sys

import serial

from aioc_ptt import AiocPtt

DEFAULT_SERIAL_PORT = "/dev/ttyACM0"
DEFAULT_ALSA_DEVICE = "plughw:AllInOneCable,0"


def parse_args():
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "--port", default=DEFAULT_SERIAL_PORT,
        help=f"AIOC serial/CDC-ACM device used for PTT (default: {DEFAULT_SERIAL_PORT})",
    )
    p.add_argument(
        "--device", default=DEFAULT_ALSA_DEVICE,
        help=f"AIOC ALSA playback device (default: {DEFAULT_ALSA_DEVICE})",
    )
    p.add_argument(
        "--wav", action="store_true",
        help="stdin is a WAV stream with a header; aplay auto-detects the "
             "format. Overrides --rate/--channels/--format.",
    )
    p.add_argument(
        "--rate", type=int, default=48000,
        help="Sample rate of raw PCM on stdin, ignored with --wav (default: 48000)",
    )
    p.add_argument(
        "--channels", type=int, default=1,
        help="Channel count of raw PCM on stdin, ignored with --wav (default: 1)",
    )
    p.add_argument(
        "--format", default="S16_LE",
        help="ALSA sample format of raw PCM on stdin, ignored with --wav (default: S16_LE)",
    )
    p.add_argument(
        "--lead-in", type=float, default=0.3,
        help="Seconds PTT is held before audio starts, lets the transmitter "
             "settle before modulation begins (default: 0.3)",
    )
    p.add_argument(
        "--tail-out", type=float, default=0.2,
        help="Seconds PTT is held after audio ends, avoids clipping the "
             "last bit of audio (default: 0.2)",
    )
    p.add_argument(
        "--max-seconds", type=float, default=300.0,
        help="Safety cutoff: force-stop playback and release PTT if the "
             "transmission runs longer than this many seconds, protects "
             "against a stdin source that never closes (default: 300)",
    )
    return p.parse_args()


def main():
    args = parse_args()

    try:
        ptt = AiocPtt(args.port, lead_in_s=args.lead_in, tail_out_s=args.tail_out)
    except serial.SerialException as e:
        print(f"Fehler: konnte {args.port} nicht oeffnen: {e}", file=sys.stderr)
        return 1

    aplay_proc = None

    def handle_signal(signum, frame):
        print(f"\nSignal {signum} empfangen, breche Sendung ab...", file=sys.stderr)
        if aplay_proc is not None and aplay_proc.poll() is None:
            aplay_proc.terminate()
        ptt.force_release()
        sys.exit(1)

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    try:
        ptt.key(on_keyed=lambda: print("PTT ON", file=sys.stderr))

        if args.wav:
            cmd = ["aplay", "-D", args.device]
        else:
            cmd = [
                "aplay", "-D", args.device,
                "-t", "raw", "-f", args.format,
                "-r", str(args.rate), "-c", str(args.channels),
            ]

        # stdin is inherited directly (not piped through Python) so audio
        # bytes stream straight into aplay with no extra buffering here.
        # aplay exits on its own once stdin hits EOF -- that is the
        # "Abbruch des Audiosignals" stop condition.
        aplay_proc = subprocess.Popen(cmd, stdin=sys.stdin)

        try:
            aplay_proc.wait(timeout=args.max_seconds)
        except subprocess.TimeoutExpired:
            print(
                f"Sicherheits-Timeout ({args.max_seconds}s) erreicht, breche ab.",
                file=sys.stderr,
            )
            aplay_proc.terminate()
            aplay_proc.wait()

        return aplay_proc.returncode

    finally:
        # Runs on every exit path: normal end, exception, or after the
        # signal handler's sys.exit() unwinds the stack. This is what
        # guarantees the transmitter never gets left keyed up.
        ptt.unkey()
        print("PTT OFF", file=sys.stderr)
        ptt.close()


if __name__ == "__main__":
    sys.exit(main() or 0)
