"""Real ALSA/PipeWire audio device enumeration for the Source combobox
(input devices, e.g. a specific mic vs. the built-in laptop mic) and for
Soundcard mode's device-connection combobox (output devices, e.g. an
external SSB transceiver's USB sound card).

Shells out to `arecord -l`/`aplay -l` (part of alsa-utils, already a
dependency of any PipeWire/ALSA desktop this app runs on) rather than the
raw `-L` listing: `-l` lists physical cards+devices only, while `-L`
explodes each one into every ALSA plugin variant (dmix/dsnoop/plughw/
surround.../hw/...), which would flood the combobox with a dozen
near-duplicate entries per real device. `LC_ALL=C` forces the standard
English output format regardless of system locale (verified: without it,
this system's own German locale prints "Karte"/"Gerät" instead of
"card"/"device", which the parser below would silently fail to match).

Device strings use ALSA's `plughw:CARD=<id>,DEV=<n>` form -- `plughw`
(not raw `hw`) lets ALSA handle sample-rate/format conversion so it
matches whatever rate the caller actually requests, and was verified
empirically this session to work directly with GNU Radio's own
`audio.source()`/`audio.sink()` blocks.
"""
import os
import re
import subprocess

from gnuradio import audio as _gr_audio

_CARD_RE = re.compile(
    r"^card (\d+): (.+?) \[([^\]]+)\], device (\d+): (.+?) \[([^\]]+)\]"
)


def _list_devices(alsa_tool: str) -> dict:
    devices = {"": "System Default"}
    try:
        env = dict(os.environ, LC_ALL="C")
        result = subprocess.run(
            [alsa_tool, "-l"], capture_output=True, text=True, timeout=5.0, env=env,
        )
    except (OSError, subprocess.TimeoutExpired):
        return devices
    for line in result.stdout.splitlines():
        m = _CARD_RE.match(line)
        if not m:
            continue
        _card_num, card_id, card_name, dev_num, _dev_id, dev_name = m.groups()
        device_str = f"plughw:CARD={card_id},DEV={dev_num}"
        label = f"{card_name} ({dev_name})" if dev_name != card_name else card_name
        devices[device_str] = label
    return devices


def list_input_devices() -> dict:
    """{"": "System Default", "plughw:CARD=PCH,DEV=0": "HDA Intel PCH (CX20632 Analog)", ...}"""
    return _list_devices("arecord")


def list_output_devices() -> dict:
    """Same shape as list_input_devices(), from `aplay -l`."""
    return _list_devices("aplay")


def probe_device(kind: str, device_str: str, sample_rate: int = 48_000):
    """Briefly open (and immediately release) an input ("input") or
    output ("output") audio device to check it's actually usable right
    now, returning the caught Exception on failure or None on success.

    Real bug found on real hardware: a rebuild-on-selection-change (see
    gui.py's _on_source_changed()) that just tries the new device
    directly, inside the SAME PlutoTxFlowgraph construction as the RF
    device sink, tears the OLD (working) flowgraph down first -- so a
    busy/unavailable audio device (e.g. one PipeWire already holds
    exclusively) doesn't just fail to switch mics, it silently drops the
    otherwise-healthy Pluto/HackRF connection too ("Could not connect to
    PlutoSDR: audio_alsa_source", PTT greyed out, RF link gone). Probing
    the audio device FIRST, before touching the existing flowgraph at
    all, lets a caller refuse the switch cleanly instead."""
    ctor = _gr_audio.source if kind == "input" else _gr_audio.sink
    try:
        dev = ctor(sample_rate, device_str, True)
        del dev
    except Exception as e:
        return e
    return None
