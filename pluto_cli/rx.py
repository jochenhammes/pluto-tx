"""`pluto-cli rx <mode>` -- one function per AdvancedRxFlowgraph demod mode.

Each mode function only builds an AdvancedRxFlowgraph with the right
kwargs/callbacks and hands off to runtime.run_rx_session() -- no RX logic
lives here that doesn't already exist in pluto_advanced_rx/flowgraph.py
itself. File Broadcast is an always-on parallel branch in the real
flowgraph (not gated by demod_mode), so it's available as an extra flag
on EVERY mode here, not a separate subcommand.

PSK31/RTTY/Meshtastic are DIFFERENT: only one digimode is ever connected/decoding at
a time (AdvancedRxFlowgraph.active_digimode, fixed at construction -- a
GNU Radio limitation, not a CLI simplification, see flowgraph.py's own
Digimodes comment), so `--digimode {psk31,rtty,meshtastic}` is a single, mutually-
exclusive selector rather than a monitor flag per mode -- see
pluto_cli/README.md."""
from pluto_advanced_rx.flowgraph import AdvancedRxFlowgraph, RADE_AVAILABLE, M17_AVAILABLE, LORA_AVAILABLE
from pluto_advanced_rx import config as rx_config
from pluto_advanced_rx import devices as rx_devices
from pluto_advanced_rx.filebroadcast_state import FileBroadcastState
from pluto_advanced_rx.meshcore_state import MeshcoreState
from pluto_advanced_rx.meshtastic_state import MeshtasticState
from pluto_advanced_rx.pocsag_state import PocsagState
from pluto_advanced_rx.psk31_state import Psk31ChatState
from pluto_advanced_rx.rtty_state import RttyChatState

from . import runtime

MODES = ("fm", "ssb", "lsb", "rade", "m17", "baseband")


def add_common_args(parser):
    parser.add_argument(
        "--device", choices=sorted(rx_devices.DEVICE_REGISTRY), default="pluto",
        help="RX hardware backend (default: pluto)",
    )
    parser.add_argument(
        "--uri", default=None,
        help="Device connection string (libiio URI for pluto, serial for hackrf/rtlsdr, ALSA "
             "device for audio). Omit to use that backend's own default.",
    )
    parser.add_argument(
        "--freq", type=float, default=rx_config.DEFAULT_FREQUENCY,
        help=f"Receive frequency in Hz (default: {rx_config.DEFAULT_FREQUENCY:.0f})",
    )
    parser.add_argument(
        "--bandwidth", type=int, choices=rx_config.RX_BANDWIDTH_PRESETS, default=None,
        help="RX bandwidth/\"zoom\" span in Hz (device sample rate). Omit for the device's own default.",
    )
    parser.add_argument(
        "--buffer-size", type=int, choices=rx_config.PLUTO_RX_BUFFER_SIZE_PRESETS, default=None,
        help="Pluto-only: client-side libiio buffer size (samples). Larger can raise achievable "
             f"throughput at the cost of latency (default: {rx_config.PLUTO_RX_BUFFER_SIZE_DEFAULT}). "
             "Ignored (harmless) on other --device backends.",
    )
    parser.add_argument(
        "--gain-mode", choices=rx_config.GAIN_MODES, default=rx_config.DEFAULT_GAIN_MODE,
        help=f"AGC mode for AGC-capable devices (default: {rx_config.DEFAULT_GAIN_MODE})",
    )
    parser.add_argument(
        "--gain", type=float, default=rx_config.DEFAULT_MANUAL_GAIN_DB,
        help=f"Manual gain in dB, used when --gain-mode manual (default: {rx_config.DEFAULT_MANUAL_GAIN_DB:.0f})",
    )
    parser.add_argument(
        "--freq-correction-ppm", type=float, default=0.0, metavar="PPM",
        help="Oscillator error of the device in ppm (default: 0). Positive = the device runs too HIGH "
             "(signals appear above their true frequency); the tool tunes the hardware lower to "
             "compensate. Scales with --freq. Example: a 100.000 MHz carrier appears at 100.003 MHz -> +30.",
    )
    parser.add_argument(
        "--direct-sampling", choices=("off", "i", "q"), default="off",
        help="RTL-SDR only: bypass the tuner and sample the antenna input directly (HF up to "
             "28.8 MHz, aliased above 14.4 MHz; the tuner gain has no effect). Choose the branch your dongle's HF input is "
             "wired to (RTL-SDR Blog V3: q). Default: off.",
    )
    parser.add_argument(
        "--audio-out", default="",
        help="Demodulated-audio output device string, from 'pluto-cli devices "
             "list-audio-outputs' (real ALSA device or the persistent qpwgraph loopback node). "
             "Empty (default) = system default speaker/sink.",
    )
    parser.add_argument(
        "--duration", type=float, default=None,
        help="Seconds to run before exiting automatically. Omit to run until Ctrl-C.",
    )
    parser.add_argument(
        "--digimode", choices=("psk31", "rtty", "meshtastic", "meshcore", "pocsag"), default=None,
        help="Also decode this digimode in parallel and print received characters as they "
             "arrive (independent of the primary --width-hz/demod mode above). Only ONE "
             "digimode can be active at a time -- a real GNU Radio limitation, not a CLI "
             "simplification (see pluto_cli/README.md). Omit to disable digimode decoding "
             "entirely.",
    )
    parser.add_argument(
        "--pocsag-charset", choices=("ascii", "de"), default="ascii",
        help="Text charset for --digimode pocsag: ascii = plain 7-bit ASCII, de = DIN 66003 German "
             "umlaut mapping (default: ascii)",
    )
    parser.add_argument(
        "--pocsag-show-damaged", action="store_true",
        help="With --digimode pocsag also report calls that contain unrecoverable codewords "
             "(default: only clean calls; damaged ones are usually junk from a weak signal)",
    )
    parser.add_argument(
        "--psk31-tone-hz", type=float, default=rx_config.PSK31_DEFAULT_TONE_HZ,
        help=f"PSK31 tone-filter center frequency in Hz, used when --digimode psk31 "
             f"(default: {rx_config.PSK31_DEFAULT_TONE_HZ:.0f})",
    )
    parser.add_argument(
        "--rtty-mark-hz", type=float, default=rx_config.RTTY_MARK_HZ_DEFAULT,
        help=f"RTTY mark tone frequency in Hz, used when --digimode rtty "
             f"(default: {rx_config.RTTY_MARK_HZ_DEFAULT:.0f})",
    )
    parser.add_argument(
        "--rtty-shift-hz", type=float, default=rx_config.RTTY_SHIFT_HZ_DEFAULT,
        help=f"RTTY mark/space shift in Hz, used when --digimode rtty "
             f"(default: {rx_config.RTTY_SHIFT_HZ_DEFAULT:.0f}, "
             f"common presets: {', '.join(f'{s:g}' for s in rx_config.RTTY_SHIFT_HZ_PRESETS)})",
    )
    parser.add_argument(
        "--rtty-baud-rate", type=float, default=rx_config.RTTY_BAUD_RATE_DEFAULT,
        help=f"RTTY baud rate, used when --digimode rtty (default: {rx_config.RTTY_BAUD_RATE_DEFAULT:g}, "
             f"common presets: {', '.join(f'{b:g}' for b in rx_config.RTTY_BAUD_RATE_PRESETS)})",
    )
    parser.add_argument(
        "--rtty-reverse", action="store_true",
        help="Swap which tone is Mark vs. Space, used when --digimode rtty",
    )
    parser.add_argument(
        "--meshtastic-region", choices=("eu433", "eu868"), default="eu868",
        help="Region for --digimode meshtastic: retunes to the preset's carrier, --freq is ignored "
             "(default: eu868).",
    )
    parser.add_argument(
        "--meshtastic-modem-preset", choices=rx_config.MESHTASTIC_MODEM_NAMES, default="LongFast",
        help="Meshtastic modem preset (SF/BW/CR), used when --digimode meshtastic (default: LongFast, "
             "the only one verified against real hardware). The RX bandwidth must be at least twice "
             "the preset's LoRa bandwidth.",
    )
    parser.add_argument(
        "--meshtastic-channel", default=None,
        help="Channel name for the header pre-filter, used when --digimode meshtastic (default: LongFast)",
    )
    parser.add_argument(
        "--meshtastic-psk", default=rx_config.MESHTASTIC_DEFAULT_PSK_B64,
        help="Channel key, base64 as in the Meshtastic apps (default: the stock channel key AQ==; "
             "empty = unencrypted), used when --digimode meshtastic",
    )
    parser.add_argument(
        "--meshcore-preset", choices=tuple(p.name for p in rx_config.MESHCORE_PRESETS),
        default=rx_config.MESHCORE_PRESETS[0].name,
        help="MeshCore preset (carrier, SF/BW/CR, preamble, sync word) for --digimode meshcore; --freq is "
             f"ignored (default: {rx_config.MESHCORE_PRESETS[0].name})",
    )
    parser.add_argument(
        "--meshcore-decrypt-dm", action="store_true",
        help="With --digimode meshcore also decrypt direct messages addressed to this app's node identity "
             "(~/.config/pluto-tx/meshcore_identity.json, created if missing); senders must have advertised "
             "before. Direct messages to other nodes stay unreadable.",
    )
    parser.add_argument(
        "--meshcore-channel", action="append", default=[], metavar="NAME:HEX",
        help="Extra MeshCore group channel to decrypt (name and 32 hex digits of the secret; repeatable). "
             "The Public channel is always decoded.",
    )
    parser.add_argument(
        "--filebroadcast-save-dir", default=None, metavar="DIR",
        help="Also watch for File Broadcast files in parallel (always-on branch, independent of "
             "the primary demod mode) and save each one to this directory as soon as it's fully "
             "received",
    )
    parser.add_argument("--json", action="store_true", help="Emit one JSON object per line instead of plain text")


def _build_and_run(args, mode, emitter, **mode_kwargs):
    psk31_state = Psk31ChatState()
    rtty_state = RttyChatState()
    filebroadcast_state = FileBroadcastState()

    def on_psk31_char(ch):
        psk31_state.on_char(ch)
        emitter.emit_char(ch)

    def on_rtty_char(ch):
        rtty_state.on_char(ch)
        emitter.emit_char(ch, event="rtty_char")

    def on_filebroadcast_frame(frame):
        if frame["type"] == "directory":
            filebroadcast_state.on_directory_frame(
                frame["file_id"], frame["filename"], frame["total_size"], frame["checksum"],
            )
        elif frame["type"] == "data":
            filebroadcast_state.on_data_frame(frame["file_id"], frame["offset"], frame["payload"])

    meshtastic_state = MeshtasticState()
    try:
        meshtastic_preset_index = rx_config.meshtastic_preset_index(args.meshtastic_modem_preset, args.meshtastic_region)
    except ValueError as e:
        emitter.error(str(e))
        return 1
    meshtastic_preset = rx_config.MESHTASTIC_PRESETS[meshtastic_preset_index]

    def on_meshtastic_frame(raw):
        meshtastic_state.on_frame(raw)
        row = meshtastic_state.get_snapshot()[1][-1]
        fields = {"kind": row["kind"], "bytes": row["raw_len"]}
        if "from" in row:
            fields.update({"from": f"{row['from']:08x}", "to": f"{row['to']:08x}", "id": row["id"],
                           "hop_limit": row["hop_limit"], "hop_start": row["hop_start"], "text": row["text"]})
        emitter.emit("meshtastic_frame", **fields)

    meshcore_state = MeshcoreState()
    meshcore_preset_index = [p.name for p in rx_config.MESHCORE_PRESETS].index(args.meshcore_preset)
    if args.digimode == "meshcore":
        from pluto_tx import meshcore_codec
        extra = []
        for spec in args.meshcore_channel:
            name, _, key = spec.partition(":")
            try:
                extra.append(meshcore_codec.GroupChannel(name or "Custom", meshcore_codec.parse_channel_secret(key)))
            except ValueError as e:
                emitter.error(f"invalid --meshcore-channel {spec!r}: {e}")
                return 1
        meshcore_state.set_channels(extra)
        if args.meshcore_decrypt_dm:
            from pluto_tx import meshcore_identity
            identity = meshcore_identity.load_or_create()
            meshcore_state.set_identity(identity)
            emitter.emit("meshcore_identity", public_key=identity.public_key.hex())

    def on_meshcore_packet(raw):
        row = meshcore_state.on_frame(raw)
        if row["kind"] == "invalid":
            return
        fields = {k: row[k] for k in ("kind", "route", "type", "hops", "verified", "raw_len") if k in row}
        for k in ("name", "role", "public_key", "latitude", "longitude", "timestamp", "channel", "sender", "text",
                  "sender_key", "dest_hash", "src_hash", "ack_hash"):
            if k in row:
                fields[k] = row[k]
        emitter.emit("meshcore_packet", **fields)

    pocsag_state = PocsagState()
    pocsag_state.set_charset(args.pocsag_charset)
    pocsag_state.set_hide_damaged(False)  # damaged calls are filtered by on_pocsag_message()

    def on_pocsag_message(msg):
        if msg["uncorrectable"] and not args.pocsag_show_damaged:
            return
        pocsag_state.on_message(msg)
        row = pocsag_state.get_snapshot()[1][-1]
        emitter.emit("pocsag_message", baud=row["baud"], ric=row["ric"], function=row["function"],
                     text=row["text"], corrected=row["corrected"], uncorrectable=row["uncorrectable"])

    def on_m17_fields(fields):
        emitter.emit("m17_fields", **runtime.json_safe(fields))

    direct_sampling = {"off": 0, "i": 1, "q": 2}[args.direct_sampling]
    if direct_sampling and not rx_devices.DEVICE_REGISTRY[args.device].supports_direct_sampling:
        emitter.error(f"--direct-sampling is only available for --device rtlsdr, not {args.device}")
        return 1
    frequency = args.freq
    if args.digimode == "meshcore":
        from pluto_advanced_rx.flowgraph import MESHCORE_AVAILABLE
        if not MESHCORE_AVAILABLE:
            emitter.error("MeshCore is not available -- gr-lora_sdr / the cryptography package is not installed, "
                          "see install-lora.sh")
            return 1
        frequency = rx_config.MESHCORE_PRESETS[meshcore_preset_index].frequency_hz
        emitter.emit("meshcore_listen", preset=args.meshcore_preset, freq_hz=frequency)
    if args.digimode == "meshtastic":
        if not LORA_AVAILABLE:
            emitter.error("Meshtastic is not available -- gr-lora_sdr / the meshtastic package is not installed, "
                          "see install-lora.sh")
            return 1
        from pluto_tx import meshtastic_codec
        try:
            psk = meshtastic_codec.parse_psk(args.meshtastic_psk)
        except ValueError as e:
            emitter.error(f"invalid --meshtastic-psk: {e}")
            return 1
        name = args.meshtastic_channel or meshtastic_preset.default_channel_name
        channels = [(name, psk)]
        if meshtastic_preset.ham_mode_required and psk:
            channels.insert(0, (name, b""))  # amateur band: unencrypted first
        meshtastic_state.set_channels(channels)
        frequency = meshtastic_preset.frequency_hz
        emitter.emit("meshtastic_listen", preset=meshtastic_preset.name, freq_hz=frequency,
                     regulatory=meshtastic_preset.regulatory_label)

    device_cls = rx_devices.DEVICE_REGISTRY[args.device]
    connection = runtime.resolve_connection(device_cls, args.uri)
    runtime.probe_or_exit(device_cls, connection, emitter)

    def _build():
        return AdvancedRxFlowgraph(
            device_type=args.device, uri=connection, frequency=frequency,
            sample_rate=args.bandwidth, buffer_size=args.buffer_size,
            gain_mode=args.gain_mode, manual_gain_db=args.gain,
            demod_mode=mode, audio_device=args.audio_out,
            active_digimode=args.digimode,
            psk31_tone_hz=args.psk31_tone_hz,
            on_psk31_char=on_psk31_char, on_filebroadcast_frame=on_filebroadcast_frame,
            on_m17_fields=on_m17_fields,
            rtty_mark_hz=args.rtty_mark_hz, rtty_shift_hz=args.rtty_shift_hz,
            rtty_baud_rate=args.rtty_baud_rate, rtty_reverse=args.rtty_reverse,
            on_rtty_char=on_rtty_char,
            on_meshtastic_frame=on_meshtastic_frame, meshtastic_preset_index=meshtastic_preset_index,
            on_pocsag_message=on_pocsag_message,
            on_meshcore_packet=on_meshcore_packet, meshcore_preset_index=meshcore_preset_index,
            frequency_correction_ppm=args.freq_correction_ppm, direct_sampling=direct_sampling,
            **mode_kwargs,
        )

    tb = runtime.build_or_exit(_build, device_cls, connection, emitter)
    runtime.install_safety_handlers(tb, emitter)
    runtime.run_rx_session(tb, args, emitter, filebroadcast_state=filebroadcast_state)
    return 0


# --- mode subparsers -------------------------------------------------

def add_fm_subparser(subparsers):
    p = subparsers.add_parser("fm", help="Narrowband FM voice", description=__doc__)
    add_common_args(p)
    p.add_argument("--width-hz", type=float, default=rx_config.FM_DEMOD_WIDTH_DEFAULT_HZ,
                   help=f"IF channel filter width in Hz (default: {rx_config.FM_DEMOD_WIDTH_DEFAULT_HZ:.0f})")
    p.set_defaults(func=run_fm)


def run_fm(args):
    return _build_and_run(
        args, AdvancedRxFlowgraph.MODE_FM, runtime.Emitter(args.json), fm_demod_width_hz=args.width_hz,
    )


def add_ssb_subparser(subparsers):
    p = subparsers.add_parser("ssb", help="SSB (USB) voice", description=__doc__)
    add_common_args(p)
    p.add_argument("--width-hz", type=float, default=rx_config.SSB_DEMOD_WIDTH_DEFAULT_HZ,
                   help=f"Demod bandpass width in Hz (default: {rx_config.SSB_DEMOD_WIDTH_DEFAULT_HZ:.0f})")
    p.set_defaults(func=run_ssb)


def run_ssb(args):
    return _build_and_run(
        args, AdvancedRxFlowgraph.MODE_SSB, runtime.Emitter(args.json), ssb_demod_width_hz=args.width_hz,
    )


def add_lsb_subparser(subparsers):
    p = subparsers.add_parser("lsb", help="SSB (LSB) voice", description=__doc__)
    add_common_args(p)
    p.add_argument("--width-hz", type=float, default=rx_config.SSB_DEMOD_WIDTH_DEFAULT_HZ,
                   help=f"Demod bandpass width in Hz (default: {rx_config.SSB_DEMOD_WIDTH_DEFAULT_HZ:.0f})")
    p.set_defaults(func=run_lsb)


def run_lsb(args):
    return _build_and_run(
        args, AdvancedRxFlowgraph.MODE_LSB, runtime.Emitter(args.json), ssb_demod_width_hz=args.width_hz,
    )


def add_rade_subparser(subparsers):
    p = subparsers.add_parser(
        "rade", help="RADE (radio autoencoder) digital voice (requires librade.so, see install-rade.sh)",
        description=__doc__,
    )
    add_common_args(p)
    p.set_defaults(func=run_rade)


def run_rade(args):
    emitter = runtime.Emitter(args.json)
    if not RADE_AVAILABLE:
        emitter.error("RADE is not available -- librade.so/lpcnet_demo not found, see install-rade.sh")
        return 1
    return _build_and_run(args, AdvancedRxFlowgraph.MODE_RADE, emitter)


def add_m17_subparser(subparsers):
    p = subparsers.add_parser(
        "m17", help="M17 digital voice (requires gr-m17, see install-m17.sh)", description=__doc__,
    )
    add_common_args(p)
    p.set_defaults(func=run_m17)


def run_m17(args):
    emitter = runtime.Emitter(args.json)
    if not M17_AVAILABLE:
        emitter.error("M17 is not available -- gr-m17 is not installed, see install-m17.sh")
        return 1
    return _build_and_run(args, AdvancedRxFlowgraph.MODE_M17, emitter)


def add_baseband_subparser(subparsers):
    p = subparsers.add_parser(
        "baseband", help="Raw, wideband, unprocessed audio passthrough (for digimode software)",
        description=__doc__,
    )
    add_common_args(p)
    lo, hi = rx_config.BASEBAND_WIDTH_RANGE_HZ
    p.add_argument(
        "--width-hz", type=float, default=rx_config.BASEBAND_WIDTH_DEFAULT_HZ,
        help=f"IF channel filter width in Hz (default: {rx_config.BASEBAND_WIDTH_DEFAULT_HZ:.0f}, "
             f"range {lo:.0f}-{hi:.0f})",
    )
    p.set_defaults(func=run_baseband)


def run_baseband(args):
    return _build_and_run(
        args, AdvancedRxFlowgraph.MODE_BASEBAND, runtime.Emitter(args.json), baseband_width_hz=args.width_hz,
    )


def add_subparsers(rx_subparsers):
    add_fm_subparser(rx_subparsers)
    add_ssb_subparser(rx_subparsers)
    add_lsb_subparser(rx_subparsers)
    add_rade_subparser(rx_subparsers)
    add_m17_subparser(rx_subparsers)
    add_baseband_subparser(rx_subparsers)
