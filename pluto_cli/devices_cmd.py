"""`pluto-cli devices ...` -- device/audio discovery utilities.

Thin wrappers around functionality the GUI apps' own Scan button and
Source/Audio-Output combos already use -- probe_with_timeout()/
scan_devices_with_timeout() (pluto_tx/pluto_advanced_rx's devices/*.py)
and pluto_tx/audio_devices.py's device-listing/persistent-loopback-node
functions (shared by both apps). No new discovery logic lives here.
"""
import json

from pluto_tx import audio_devices
from pluto_tx import devices as tx_devices
from pluto_advanced_rx import devices as rx_devices


def _print_devices(found: dict, as_json: bool):
    if as_json:
        print(json.dumps(found))
        return
    if not found:
        print("(no devices found)")
        return
    for connection, label in found.items():
        shown = connection if connection else '""  (system default)'
        print(f"{shown}\t{label}")


def add_scan_subparser(subparsers):
    p = subparsers.add_parser(
        "scan", help="Scan for TX or RX hardware devices",
        description="Scan for TX or RX hardware devices, using the exact same "
                     "scan_devices_with_timeout() each GUI app's Scan button already calls.",
    )
    group = p.add_mutually_exclusive_group(required=True)
    group.add_argument("--tx", action="store_true", help="Scan pluto_tx's device backends")
    group.add_argument("--rx", action="store_true", help="Scan pluto_advanced_rx's device backends")
    p.add_argument("--device", required=True, help="Backend to scan (e.g. pluto, hackrf, rtlsdr, soundcard, audio)")
    p.add_argument("--timeout", type=float, default=5.0, help="Scan timeout in seconds (default: 5.0)")
    p.add_argument("--json", action="store_true", help="Print as a JSON object instead of a table")
    p.set_defaults(func=run_scan)


def run_scan(args):
    registry = tx_devices.DEVICE_REGISTRY if args.tx else rx_devices.DEVICE_REGISTRY
    if args.device not in registry:
        print(f"Unknown device '{args.device}' for {'--tx' if args.tx else '--rx'} -- choices: {sorted(registry)}")
        return 1
    device_cls = registry[args.device]
    found, error = device_cls.scan_devices_with_timeout(args.timeout)
    if error is not None:
        print(f"Scan failed: {error}")
        return 1
    _print_devices(found, args.json)
    return 0


def add_probe_subparser(subparsers):
    p = subparsers.add_parser(
        "probe", help="Check whether a specific TX or RX device connection is reachable right now",
        description="Probe a specific device connection string, using the exact same "
                     "probe_with_timeout() each GUI app calls before tearing down an existing "
                     "connection to switch devices.",
    )
    group = p.add_mutually_exclusive_group(required=True)
    group.add_argument("--tx", action="store_true")
    group.add_argument("--rx", action="store_true")
    p.add_argument("--device", required=True, help="Backend (e.g. pluto, hackrf, rtlsdr, soundcard, audio)")
    p.add_argument("--connection", required=True, help="Connection string to probe (e.g. an IP/URI or serial)")
    p.add_argument("--timeout", type=float, default=5.0, help="Probe timeout in seconds (default: 5.0)")
    p.set_defaults(func=run_probe)


def run_probe(args):
    registry = tx_devices.DEVICE_REGISTRY if args.tx else rx_devices.DEVICE_REGISTRY
    device_cls = registry[args.device]
    error = device_cls.probe_with_timeout(args.connection, args.timeout)
    if error is None:
        print("OK -- reachable")
        return 0
    print(f"NOT reachable: {error}")
    return 1


def add_list_audio_inputs_subparser(subparsers):
    p = subparsers.add_parser(
        "list-audio-inputs", help="List real ALSA input (microphone) devices",
        description="List real ALSA input devices -- the same list pluto_tx's Source combo shows.",
    )
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=lambda args: (_print_devices(audio_devices.list_input_devices(), args.json), 0)[1])


def add_list_audio_outputs_subparser(subparsers):
    p = subparsers.add_parser(
        "list-audio-outputs", help="List real ALSA output (speaker) devices",
        description="List real ALSA output devices -- the same list pluto_advanced_rx's Audio "
                     "Output combo (and pluto_tx's Soundcard-mode output combo) shows.",
    )
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=lambda args: (_print_devices(audio_devices.list_output_devices(), args.json), 0)[1])


def add_list_audio_monitors_subparser(subparsers):
    p = subparsers.add_parser(
        "list-audio-monitors", help="List PipeWire sink monitors (capture whatever's playing on a sink)",
        description="List PipeWire sink monitors -- lets you capture whatever audio is currently "
                     "PLAYING on a sink (e.g. another app's output) as an input, without an "
                     "acoustic speaker-to-mic loopback.",
    )
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=lambda args: (_print_devices(audio_devices.list_monitor_devices(), args.json), 0)[1])


def add_ensure_loopback_subparser(subparsers):
    p = subparsers.add_parser(
        "ensure-loopback",
        help="Create (if missing) and print the device string for a persistent qpwgraph loopback node",
        description="Create (if missing) and print the device string for one of the four "
                     "persistent, app-managed PipeWire loopback nodes -- the same stable "
                     "qpwgraph patch points both GUI apps create automatically at startup. "
                     "Useful for pre-provisioning a patch point from a script before either "
                     "GUI app is even running.",
    )
    group = p.add_mutually_exclusive_group(required=True)
    group.add_argument("--tx-input", action="store_true", help="pluto-tx's own audio INPUT node")
    group.add_argument("--tx-output", action="store_true", help="pluto-tx's own audio OUTPUT node")
    group.add_argument("--rx-input", action="store_true", help="pluto-advanced-rx's own audio INPUT node")
    group.add_argument("--rx-output", action="store_true", help="pluto-advanced-rx's own audio OUTPUT node")
    p.add_argument("--name", default=None, help="Override the node's base name (default: the app's own convention)")
    p.set_defaults(func=run_ensure_loopback)


def run_ensure_loopback(args):
    if args.tx_input:
        kwargs = {} if args.name is None else {"name": args.name}
        device_str = audio_devices.ensure_persistent_input_node(**kwargs)
    elif args.tx_output:
        kwargs = {} if args.name is None else {"name": args.name}
        device_str = audio_devices.ensure_persistent_output_node(**kwargs)
    elif args.rx_input:
        device_str = audio_devices.ensure_persistent_input_node(name=args.name or "pluto-advanced-rx-input")
    else:
        device_str = audio_devices.ensure_persistent_output_node(name=args.name or "pluto-advanced-rx-output")

    if device_str is None:
        print("Failed -- PipeWire tooling (pw-loopback/pw-dump/pw-link) not available")
        return 1
    print(device_str)
    return 0


def add_subparsers(devices_subparsers):
    add_scan_subparser(devices_subparsers)
    add_probe_subparser(devices_subparsers)
    add_list_audio_inputs_subparser(devices_subparsers)
    add_list_audio_outputs_subparser(devices_subparsers)
    add_list_audio_monitors_subparser(devices_subparsers)
    add_ensure_loopback_subparser(devices_subparsers)
