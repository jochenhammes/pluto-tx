"""Where the tools keep their settings, and what every front end (the command line, other programs that use the transmitter) needs to know
about them: the licence acceptance, and the thermal model file of a board."""
import os
from pathlib import Path

LICENCE_NOTICE = """\
Transmitting takes an amateur radio licence, and you are responsible for what you send. This transmitter is a Wi-Fi chip's
test tone, steered in frequency and amplitude; it was measured on one board only. Its power is not calibrated (some microwatts
at the antenna of a plain dev board, an estimate), its harmonics and spurious emissions were not measured, and its frequency
is only as good as the board's crystal (up to +-24 kHz; correct it with --ppm). It sends only inside 2320..2450 MHz.
"""


def config_dir():
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "espdr"


def licence_accepted():
    return (config_dir() / "licence-accepted").exists()


def accept_licence(version=""):
    d = config_dir()
    d.mkdir(parents=True, exist_ok=True)
    (d / "licence-accepted").write_text(f"accepted with espdr-tx {version}\n")


def model_names(bridge_port=None):
    """The names of the thermal model files of the board, best first: thermal-<serial number of its USB-UART bridge>.json, then
    thermal.json. (The native port reports the same serial number on every board while the firmware runs, the bridge's differs.) With
    several boards attached the bridge must be named with `bridge_port`."""
    from serial.tools import list_ports

    from . import board
    bridges = [p for p in list_ports.comports() if (p.vid, p.pid) in board.BRIDGES and p.serial_number]
    if bridge_port:
        bridges = [p for p in bridges if p.device == bridge_port]
    return ([f"thermal-{bridges[0].serial_number}.json"] if len(bridges) == 1 else []) + ["thermal.json"]


def board_model(bridge_port=None):
    """The path of the board's thermal model file, or None if it has none."""
    for n in model_names(bridge_port):
        if (config_dir() / n).exists():
            return str(config_dir() / n)
    return None
