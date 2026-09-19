"""`pluto-cli tx <mode>` -- one function per PlutoTxFlowgraph mode.

Each mode function only builds a PlutoTxFlowgraph with the right kwargs
and hands off to runtime.run_tx_session() -- no TX logic lives here that
doesn't already exist in pluto_tx/flowgraph.py itself. See pluto_cli/
README.md for the full command reference and examples.
"""
import argparse
import sys

from pluto_tx.flowgraph import PlutoTxFlowgraph, M17_AVAILABLE, FREEDV_AVAILABLE, RADE_AVAILABLE, LORA_AVAILABLE
from pluto_tx import config as tx_config
from pluto_tx import dcs as tx_dcs
from pluto_tx import pocsag_codec as tx_pocsag
from pluto_tx import devices as tx_devices

from . import runtime

MODES = ("fm", "ssb", "lsb", "m17", "freedv", "rade", "digitext", "psk31", "rtty", "pocsag", "meshtastic", "filebroadcast", "baseband")


def add_common_args(parser):
    parser.add_argument(
        "--device", choices=sorted(tx_devices.DEVICE_REGISTRY), default="pluto",
        help="TX hardware backend (default: pluto)",
    )
    parser.add_argument(
        "--uri", default=None,
        help="Device connection string (libiio URI for pluto, serial for hackrf, blank for "
             "soundcard's system default). Omit to use that backend's own default.",
    )
    parser.add_argument(
        "--freq", type=float, default=tx_config.DEFAULT_FREQUENCY,
        help=f"Transmit frequency in Hz (default: {tx_config.DEFAULT_FREQUENCY:.0f})",
    )
    parser.add_argument(
        "--freq-correction-ppm", type=float, default=0.0, metavar="PPM",
        help="Oscillator error of the device in ppm (default: 0). Positive = the device runs too "
             "HIGH, the tool tunes the hardware lower to compensate; scales with --freq. Example: a "
             "receiver shows 432.150 MHz at 432.125 MHz -> device is ~58 ppm low -> -58.",
    )
    parser.add_argument(
        "--power-ceiling", type=float, default=None,
        help="Maximum TX power/attenuation in dB (device-specific meaning -- e.g. Pluto's "
             "attenuation, negative dB). Omit for the device's own safe default.",
    )
    parser.add_argument(
        "--power", type=float, default=None,
        help="Target TX power within --power-ceiling (default: equal to the ceiling, i.e. max "
             "power allowed)",
    )
    parser.add_argument(
        "--source", choices=["mic", "file"], default="mic",
        help="Audio source for modes that transmit live/file audio (default: mic; ignored for "
             "text-based/file-based modes: digitext, psk31, rtty, filebroadcast)",
    )
    parser.add_argument("--wav-file", default=None, help="WAV file path, when --source file")
    parser.add_argument(
        "--audio-device", default="",
        help="Mic device string, from 'pluto-cli devices list-audio-inputs' (real ALSA device, "
             "a 'monitor:...' PipeWire sink monitor, or the persistent qpwgraph loopback node). "
             "Empty (default) = system default microphone.",
    )
    parser.add_argument(
        "--duration", type=float, default=3.0,
        help="Seconds to stay keyed (default: 3.0). Ignored for digitext/psk31/rtty/pocsag, which "
             "transmit their text exactly once and compute their own duration; ignored with "
             "--interactive.",
    )
    parser.add_argument(
        "--interactive", action="store_true",
        help="Enter to key up, Enter again to unkey, repeatedly; Ctrl-C to quit. Ignores --duration.",
    )
    parser.add_argument("--yes", action="store_true", help='Skip the "Type YES to key up" confirmation prompt')
    parser.add_argument("--json", action="store_true", help="Emit one JSON object per line instead of plain text")


def _repeat_count(text):
    n = int(text)
    if not 1 <= n <= tx_config.REPEAT_COUNT_MAX:
        raise argparse.ArgumentTypeError(f"must be 1..{tx_config.REPEAT_COUNT_MAX}")
    return n


def _repeat_interval(text):
    x = float(text)
    lo, hi = tx_config.REPEAT_INTERVAL_RANGE_S
    if not lo <= x <= hi:
        raise argparse.ArgumentTypeError(f"must be {lo:g}..{hi:g} seconds")
    return x


def add_repeat_args(parser):
    """Repeat series for the one-shot modes (digitext, psk31, rtty, pocsag, meshtastic)."""
    parser.add_argument(
        "--repeat-count", type=_repeat_count, default=1, metavar="N",
        help=f"Transmit the message N times in total (1-{tx_config.REPEAT_COUNT_MAX}, default 1 = once). "
             "Ctrl-C ends the series safely.",
    )
    parser.add_argument(
        "--repeat-interval", type=_repeat_interval, default=tx_config.REPEAT_INTERVAL_DEFAULT_S, metavar="SECONDS",
        help="Pause between the end of one transmission and the start of the next "
             f"(default {tx_config.REPEAT_INTERVAL_DEFAULT_S:g}; the transmitter is off in between)",
    )


def _build(args, mode, connection, **mode_kwargs):
    source = PlutoTxFlowgraph.SRC_FILE if args.source == "file" else PlutoTxFlowgraph.SRC_MIC
    return PlutoTxFlowgraph(
        device_type=args.device, connection=connection, frequency=args.freq,
        power_ceiling=args.power_ceiling, audio_device=args.audio_device,
        wav_path=args.wav_file, mode=mode, source=source,
        **mode_kwargs,
    )


def _run(args, mode, emitter, post_construct=None, **mode_kwargs):
    device_cls = tx_devices.DEVICE_REGISTRY[args.device]
    connection = runtime.resolve_connection(device_cls, args.uri)
    runtime.probe_or_exit(device_cls, connection, emitter)
    tb = runtime.build_or_exit(lambda: _build(args, mode, connection, **mode_kwargs), device_cls, connection, emitter)
    runtime.install_safety_handlers(tb, emitter)
    if post_construct is not None:
        post_construct(tb)
    if args.power is not None:
        tb.set_target_power(args.power)
    if args.freq_correction_ppm:
        tb.device.set_frequency_correction_ppm(args.freq_correction_ppm)
    tb.start()
    runtime.confirm_or_exit(args.yes, emitter)
    runtime.run_tx_session(tb, mode, args, emitter)
    return 0


# --- mode subparsers -------------------------------------------------

def add_fm_subparser(subparsers):
    p = subparsers.add_parser("fm", help="Narrowband FM voice", description=__doc__)
    add_common_args(p)
    tone = p.add_mutually_exclusive_group()
    tone.add_argument(
        "--ctcss", type=_ctcss_tone, metavar="HZ",
        help="Send a CTCSS sub-audible tone (standard EIA tones, e.g. 88.5, 123.0)",
    )
    tone.add_argument(
        "--dcs", type=_dcs_code, metavar="CODE[N|I]",
        help="Send a DCS code (one of the 83 standard octal codes, e.g. 023, 754I; N = normal, I = inverted)",
    )
    lo, hi = tx_config.SUBTONE_LEVEL_RANGE_PCT
    p.add_argument(
        "--tone-level", type=int, default=tx_config.SUBTONE_LEVEL_DEFAULT_PCT, metavar="PCT",
        help=f"Tone deviation in %% of the FM deviation (default {tx_config.SUBTONE_LEVEL_DEFAULT_PCT}, "
             f"range {lo}-{hi}); the voice is reduced by the same amount",
    )
    p.set_defaults(func=run_fm)


def _ctcss_tone(text):
    try:
        hz = float(text.replace(",", "."))
    except ValueError:
        raise argparse.ArgumentTypeError(f"invalid CTCSS tone {text!r}")
    if not any(abs(hz - t) < 0.05 for t in tx_config.CTCSS_TONES_HZ):
        raise argparse.ArgumentTypeError(
            f"{text} Hz is not a standard CTCSS tone ({tx_config.CTCSS_TONES_HZ[0]:g} ... "
            f"{tx_config.CTCSS_TONES_HZ[-1]:g} Hz, e.g. 88.5, 123.0)")
    return min(tx_config.CTCSS_TONES_HZ, key=lambda t: abs(t - hz))


def _dcs_code(text):
    try:
        return tx_dcs.parse_code(text)
    except ValueError as e:
        raise argparse.ArgumentTypeError(str(e))


def run_fm(args):
    lo, hi = tx_config.SUBTONE_LEVEL_RANGE_PCT
    if not lo <= args.tone_level <= hi:
        print(f"--tone-level must be between {lo} and {hi}", file=sys.stderr)
        return 2

    def post_construct(tb):
        tb.set_subtone_level(args.tone_level)
        if args.ctcss is not None:
            tb.set_subtone("ctcss", args.ctcss)
        elif args.dcs is not None:
            tb.set_subtone("dcs", args.dcs)

    return _run(args, PlutoTxFlowgraph.MODE_FM, runtime.Emitter(args.json), post_construct=post_construct)


def add_ssb_subparser(subparsers):
    p = subparsers.add_parser("ssb", help="SSB (USB) voice", description=__doc__)
    add_common_args(p)
    p.set_defaults(func=run_ssb)


def run_ssb(args):
    return _run(args, PlutoTxFlowgraph.MODE_SSB, runtime.Emitter(args.json))


def add_lsb_subparser(subparsers):
    p = subparsers.add_parser("lsb", help="SSB (LSB) voice", description=__doc__)
    add_common_args(p)
    p.set_defaults(func=run_lsb)


def run_lsb(args):
    return _run(args, PlutoTxFlowgraph.MODE_LSB, runtime.Emitter(args.json))


def add_m17_subparser(subparsers):
    p = subparsers.add_parser(
        "m17", help="M17 digital voice (requires gr-m17, see install-m17.sh)", description=__doc__,
    )
    add_common_args(p)
    p.add_argument("--src-callsign", default="", help="Source callsign (max 9 chars)")
    p.add_argument("--dst-callsign", default=tx_config.M17_DEFAULT_DST_CALLSIGN,
                   help=f"Destination callsign (default: {tx_config.M17_DEFAULT_DST_CALLSIGN})")
    p.set_defaults(func=run_m17)


def run_m17(args):
    emitter = runtime.Emitter(args.json)
    if not M17_AVAILABLE:
        emitter.error("M17 is not available -- gr-m17 is not installed, see install-m17.sh")
        return 1
    return _run(
        args, PlutoTxFlowgraph.MODE_M17, emitter,
        m17_src_callsign=args.src_callsign, m17_dst_callsign=args.dst_callsign,
    )


def add_freedv_subparser(subparsers):
    p = subparsers.add_parser(
        "freedv", help="FreeDV 2020/2020B digital voice", description=__doc__,
    )
    add_common_args(p)
    p.add_argument("--variant", choices=["2020", "2020b"], default="2020", help="FreeDV variant (default: 2020)")
    p.add_argument("--callsign", default="", help="Callsign embedded in the FreeDV frame")
    p.set_defaults(func=run_freedv)


def run_freedv(args):
    from pluto_tx import freedv_ctypes

    emitter = runtime.Emitter(args.json)
    if not FREEDV_AVAILABLE:
        emitter.error("FreeDV is not available -- libcodec2 on this system lacks 2020/2020B support")
        return 1
    variant = freedv_ctypes.FREEDV_MODE_2020B if args.variant == "2020b" else freedv_ctypes.FREEDV_MODE_2020
    return _run(
        args, PlutoTxFlowgraph.MODE_FREEDV, emitter,
        freedv_variant=variant, freedv_callsign=args.callsign,
    )


def add_rade_subparser(subparsers):
    p = subparsers.add_parser(
        "rade", help="RADE (radio autoencoder) digital voice (requires librade.so, see install-rade.sh)",
        description=__doc__,
    )
    add_common_args(p)
    p.add_argument(
        "--eoo", action="store_true",
        help="Send an End-Of-Over marker after unkeying and wait for it to finish before shutting down",
    )
    p.set_defaults(func=run_rade)


def run_rade(args):
    emitter = runtime.Emitter(args.json)
    if not RADE_AVAILABLE:
        emitter.error("RADE is not available -- librade.so/lpcnet_demo not found, see install-rade.sh")
        return 1

    def post_construct(tb):
        if args.eoo:
            tb.set_rade_eoo_enabled(True)

    return _run(args, PlutoTxFlowgraph.MODE_RADE, emitter, post_construct=post_construct)


def add_digitext_subparser(subparsers):
    p = subparsers.add_parser("digitext", help="Waterfall Writer text digimode", description=__doc__)
    add_common_args(p)
    add_repeat_args(p)
    p.add_argument("--text", required=True, help="Text to draw into the waterfall")
    p.add_argument("--layout", choices=["horizontal", "vertical"], default="horizontal")
    p.add_argument("--zoom", type=int, default=1)
    p.add_argument("--min-freq-hz", type=float, default=tx_config.DIGITEXT_MIN_FREQ_HZ)
    p.set_defaults(func=run_digitext)


def run_digitext(args):
    from pluto_tx import digitext
    layout = digitext.LAYOUT_VERTICAL if args.layout == "vertical" else digitext.LAYOUT_HORIZONTAL
    return _run(
        args, PlutoTxFlowgraph.MODE_DIGITEXT, runtime.Emitter(args.json),
        digitext_text=args.text, digitext_layout=layout, digitext_zoom=args.zoom,
        digitext_min_freq_hz=args.min_freq_hz,
    )


def add_psk31_subparser(subparsers):
    p = subparsers.add_parser("psk31", help="BPSK31 keyboard-chat digimode", description=__doc__)
    add_common_args(p)
    add_repeat_args(p)
    p.add_argument("--text", required=True, help="Text to send")
    p.add_argument("--tone-hz", type=float, default=tx_config.PSK31_DEFAULT_TONE_HZ,
                   help=f"Audio tone frequency in Hz (default: {tx_config.PSK31_DEFAULT_TONE_HZ:.0f})")
    p.set_defaults(func=run_psk31)


def run_psk31(args):
    return _run(
        args, PlutoTxFlowgraph.MODE_PSK31, runtime.Emitter(args.json),
        psk31_text=args.text, psk31_tone_hz=args.tone_hz,
    )


def add_rtty_subparser(subparsers):
    p = subparsers.add_parser("rtty", help="RTTY (2-tone FSK Baudot) digimode", description=__doc__)
    add_common_args(p)
    add_repeat_args(p)
    p.add_argument("--text", required=True, help="Text to send")
    p.add_argument("--mark-hz", type=float, default=tx_config.RTTY_MARK_HZ_DEFAULT,
                   help=f"Mark tone frequency in Hz (default: {tx_config.RTTY_MARK_HZ_DEFAULT:.0f})")
    p.add_argument("--shift-hz", type=float, default=tx_config.RTTY_SHIFT_HZ_DEFAULT,
                   help=f"Mark/Space tone separation in Hz (default: {tx_config.RTTY_SHIFT_HZ_DEFAULT:.0f}, "
                        f"common presets: {', '.join(f'{s:g}' for s in tx_config.RTTY_SHIFT_HZ_PRESETS)})")
    p.add_argument("--baud-rate", type=float, default=tx_config.RTTY_BAUD_RATE_DEFAULT,
                   help=f"Baud rate (default: {tx_config.RTTY_BAUD_RATE_DEFAULT:g}, "
                        f"common presets: {', '.join(f'{b:g}' for b in tx_config.RTTY_BAUD_RATE_PRESETS)})")
    p.add_argument("--reverse", action="store_true",
                   help="Swap which tone plays Mark vs. Space (for counterpart stations with flipped polarity)")
    p.set_defaults(func=run_rtty)


def run_rtty(args):
    return _run(
        args, PlutoTxFlowgraph.MODE_RTTY, runtime.Emitter(args.json),
        rtty_text=args.text, rtty_mark_hz=args.mark_hz, rtty_shift_hz=args.shift_hz,
        rtty_baud_rate=args.baud_rate, rtty_reverse=args.reverse,
    )


def add_pocsag_subparser(subparsers):
    p = subparsers.add_parser(
        "pocsag", help="POCSAG paging call (ITU-R M.584, FM +-4.5 kHz, 512/1200/2400 baud) to a RIC",
        description=__doc__,
    )
    add_common_args(p)
    add_repeat_args(p)
    p.add_argument("--ric", type=int, default=tx_config.POCSAG_DEFAULT_RIC,
                   help=f"Pager address (RIC) 0-{tx_pocsag.RIC_MAX} (default: {tx_config.POCSAG_DEFAULT_RIC}, a test RIC)")
    p.add_argument("--kind", choices=("alpha", "numeric", "tone"), default="alpha",
                   help="Message type: alphanumeric text, numeric (0-9 * U space - [ ]) or tone only (default: alpha)")
    p.add_argument("--text", default="", help="Message text (not needed for --kind tone)")
    p.add_argument("--function", type=int, choices=(0, 1, 2, 3), default=3,
                   help="Function bits of the address codeword (default: 3; networks usually use 3 for text, 0 for numeric)")
    p.add_argument("--baud", type=int, choices=tx_pocsag.BAUD_RATES, default=tx_config.POCSAG_BAUD_DEFAULT,
                   help=f"Bit rate (default: {tx_config.POCSAG_BAUD_DEFAULT})")
    p.add_argument("--charset", choices=tx_pocsag.CHARSETS, default="ascii",
                   help="ascii = plain 7-bit ASCII; de = DIN 66003 German umlaut mapping (default: ascii)")
    p.set_defaults(func=run_pocsag)


def run_pocsag(args):
    emitter = runtime.Emitter(args.json)
    if args.device == "soundcard":
        emitter.error("POCSAG needs an RF device (pluto or hackrf), not the soundcard")
        return 1

    def preflight(tb):
        problem = tb.pocsag_problem()
        if problem:  # before any RF action
            emitter.error(problem)
            tb.shutdown_safe()
            sys.exit(1)
        tb._ensure_pocsag_audio()
        emitter.emit("pocsag_message", ric=args.ric, function=args.function, kind=args.kind, baud=args.baud,
                     text=args.text if args.kind != "tone" else "", duration_s=round(tb.pocsag_duration_s, 3))

    return _run(
        args, PlutoTxFlowgraph.MODE_POCSAG, emitter, post_construct=preflight,
        pocsag_ric=args.ric, pocsag_function=args.function, pocsag_kind=args.kind, pocsag_text=args.text,
        pocsag_baud=args.baud, pocsag_charset=args.charset,
    )


def add_meshtastic_subparser(subparsers):
    p = subparsers.add_parser(
        "meshtastic",
        help="Meshtastic (LoRa) text broadcast -- one frame (requires gr-lora_sdr, see install-lora.sh)",
        description=__doc__,
    )
    add_common_args(p)
    add_repeat_args(p)
    p.add_argument("--text", required=True, help="Message to broadcast")
    p.add_argument("--region", choices=("eu433", "eu868"), default="eu868",
                   help="Region/band; sets the carrier from the firmware's slot formula, --freq is "
                        "ignored (default: eu868). eu433 = Ham Mode (needs --callsign, no encryption); "
                        "eu868 = ISM/SRD, 10%% duty cycle enforced")
    p.add_argument("--modem-preset", choices=tx_config.MESHTASTIC_MODEM_NAMES, default="LongFast",
                   help="Meshtastic modem preset = SF/BW/CR combination (default: LongFast). Only "
                        "LongFast is verified against real hardware. No 500 kHz presets on eu868.")
    p.add_argument("--callsign", default="", help="Your callsign -- required on eu433 (appended to the text)")
    p.add_argument("--node-id", default=None, metavar="HEX",
                   help="Sender node number in hex, e.g. 5a1d0f42 (default: random)")
    p.add_argument("--channel", default=None, help="Channel name (default: LongFast)")
    p.add_argument("--psk", default=tx_config.MESHTASTIC_DEFAULT_PSK_B64,
                   help="Channel key, base64 as in the Meshtastic apps (default: the stock channel key "
                        "AQ==; empty = no encryption; ignored on eu433)")
    p.add_argument("--hop-limit", type=int, default=tx_config.MESHTASTIC_DEFAULT_HOP_LIMIT,
                   help=f"Hop limit, 0-{tx_config.MESHTASTIC_MAX_HOP_LIMIT} "
                        f"(default: {tx_config.MESHTASTIC_DEFAULT_HOP_LIMIT}); 0 = direct neighbours only")
    p.set_defaults(func=run_meshtastic)


def run_meshtastic(args):
    emitter = runtime.Emitter(args.json)
    if not LORA_AVAILABLE:
        emitter.error("Meshtastic is not available -- gr-lora_sdr / the meshtastic package is not installed, "
                      "see install-lora.sh")
        return 1
    try:
        node_id = int(args.node_id, 16) if args.node_id else None
    except ValueError:
        emitter.error(f"invalid --node-id {args.node_id!r} (expected hex)")
        return 1
    try:
        preset_index = tx_config.meshtastic_preset_index(args.modem_preset, args.region)
    except ValueError as e:
        emitter.error(str(e))
        return 1
    hop_limit = max(0, min(tx_config.MESHTASTIC_MAX_HOP_LIMIT, args.hop_limit))

    def preflight(tb):
        # every refusal (empty text, bad PSK, missing Ham-Mode callsign, duty cycle...) BEFORE any RF action
        ok, message, info = tb.prepare_meshtastic_tx()
        if not ok:
            emitter.error(message)
            tb.shutdown_safe()
            sys.exit(1)
        emitter.emit("meshtastic_frame", preset=info["preset"].name, freq_hz=info["preset"].frequency_hz,
                     text=info["text"], bytes=len(info["packet"]), airtime_s=round(info["airtime_s"], 3))

    return _run(
        args, PlutoTxFlowgraph.MODE_MESHTASTIC, emitter, post_construct=preflight,
        meshtastic_preset_index=preset_index, meshtastic_text=args.text, meshtastic_node_id=node_id,
        meshtastic_channel_name=args.channel, meshtastic_psk_b64=args.psk,
        meshtastic_hop_limit=hop_limit, meshtastic_callsign=args.callsign,
    )


def add_filebroadcast_subparser(subparsers):
    p = subparsers.add_parser(
        "filebroadcast", help="Repetitive file broadcast (round-robin, gap-fill)", description=__doc__,
    )
    add_common_args(p)
    p.add_argument(
        "--file", dest="files", action="append", required=True, metavar="PATH",
        help="File to broadcast; repeat --file for multiple files (round-robin rotation)",
    )
    p.set_defaults(func=run_filebroadcast)


def run_filebroadcast(args):
    import os

    emitter = runtime.Emitter(args.json)

    def post_construct(tb):
        for path in args.files:
            with open(path, "rb") as f:
                data = f.read()
            file_id = tb.add_filebroadcast_file(os.path.basename(path), data)
            emitter.emit("filebroadcast_added", file_id=file_id, filename=os.path.basename(path), bytes=len(data))

    return _run(args, PlutoTxFlowgraph.MODE_FILEBROADCAST, emitter, post_construct=post_construct)


def add_baseband_subparser(subparsers):
    p = subparsers.add_parser(
        "baseband", help="Raw, wideband, unprocessed audio passthrough (for digimode software)",
        description=__doc__,
    )
    add_common_args(p)
    lo, hi = tx_config.BASEBAND_DEVIATION_RANGE_HZ
    p.add_argument(
        "--deviation-hz", type=float, default=tx_config.BASEBAND_DEVIATION_HZ,
        help=f"FM deviation in Hz, sized via Carson's rule for your audio content's bandwidth "
             f"(default: {tx_config.BASEBAND_DEVIATION_HZ:.0f}, range {lo:.0f}-{hi:.0f})",
    )
    p.set_defaults(func=run_baseband)


def run_baseband(args):
    def post_construct(tb):
        if args.deviation_hz != tx_config.BASEBAND_DEVIATION_HZ:
            tb.set_baseband_deviation(args.deviation_hz)

    return _run(
        args, PlutoTxFlowgraph.MODE_BASEBAND, runtime.Emitter(args.json), post_construct=post_construct,
    )


def add_subparsers(tx_subparsers):
    add_fm_subparser(tx_subparsers)
    add_ssb_subparser(tx_subparsers)
    add_lsb_subparser(tx_subparsers)
    add_m17_subparser(tx_subparsers)
    add_freedv_subparser(tx_subparsers)
    add_rade_subparser(tx_subparsers)
    add_digitext_subparser(tx_subparsers)
    add_psk31_subparser(tx_subparsers)
    add_rtty_subparser(tx_subparsers)
    add_pocsag_subparser(tx_subparsers)
    add_meshtastic_subparser(tx_subparsers)
    add_filebroadcast_subparser(tx_subparsers)
    add_baseband_subparser(tx_subparsers)
