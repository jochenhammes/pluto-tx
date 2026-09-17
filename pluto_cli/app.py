"""pluto-cli entry point -- wires the `tx`/`rx`/`devices` subcommand
trees together and dispatches to whichever `run_*`/`func` each leaf
subparser registered. Run `pluto-cli --help`, `pluto-cli tx --help`/
`pluto-cli rx --help`/`pluto-cli devices --help`, or e.g.
`pluto-cli tx m17 --help` for the full reference at each level -- every
level's --help is written to stand alone. See pluto_cli/README.md for
the complete guide (installation, recipes, JSON schema, safety model).
"""
import argparse
import sys

from . import devices_cmd, rx, tx


def build_parser():
    parser = argparse.ArgumentParser(
        prog="pluto-cli",
        description="Headless CLI for the pluto-tx / pluto-advanced-rx GNU Radio apps -- "
                     "reuses their existing flowgraph/device code directly (see "
                     "pluto_cli/README.md), runs as its own process so it never interferes "
                     "with either GUI app.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    tx_parser = subparsers.add_parser("tx", help="Transmit (pluto_tx.flowgraph.PlutoTxFlowgraph)")
    tx_subparsers = tx_parser.add_subparsers(dest="mode", required=True, metavar="MODE")
    tx.add_subparsers(tx_subparsers)

    rx_parser = subparsers.add_parser("rx", help="Receive (pluto_advanced_rx.flowgraph.AdvancedRxFlowgraph)")
    rx_subparsers = rx_parser.add_subparsers(dest="mode", required=True, metavar="MODE")
    rx.add_subparsers(rx_subparsers)

    devices_parser = subparsers.add_parser("devices", help="Hardware/audio device discovery utilities")
    devices_subparsers = devices_parser.add_subparsers(dest="subcommand", required=True, metavar="SUBCOMMAND")
    devices_cmd.add_subparsers(devices_subparsers)

    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args) or 0
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
