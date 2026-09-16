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

A third category, PipeWire sink MONITORS (capturing whatever audio is
*playing* on a sink, e.g. another app's output, as an input -- lets
external audio feed the TX chain without a speaker-to-mic acoustic
loopback), works completely differently: native PipeWire has no
separate "monitor source" node the way PulseAudio does (`pactl` isn't
even installed on this system) -- a capture client reaches a sink's
monitor by targeting that SINK's own node via the `PIPEWIRE_NODE`
environment variable, opened through the "pipewire" ALSA PCM plugin
(NOT a `plughw:...`/`hw:...` string). Verified empirically this
session: setting `PIPEWIRE_NODE=<sink node.name>` before
`audio.source(rate, "pipewire", True)`, the captured RMS tracked a
`pw-play --target <that same sink>` play-through exactly (0.08 ->
0.20, peak 0.96), confirming real monitor audio, not noise. Device
strings for this category are represented as `"monitor:<node.name>"` --
a convention private to this module (see open_input_device()/
open_output_device() below) -- so callers elsewhere keep treating every
device string as an opaque, directly-usable value.
"""
import json
import os
import re
import subprocess
import time

from gnuradio import audio as _gr_audio

_PERSISTENT_NODE_NAME = "pluto-tx-input"

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


def _pw_dump():
    """Parsed `pw-dump` JSON (the full current PipeWire graph), or None
    on any failure (tool missing, timeout, malformed output) -- shared
    by list_monitor_devices() and ensure_persistent_input_node()."""
    try:
        result = subprocess.run(["pw-dump"], capture_output=True, text=True, timeout=5.0)
        return json.loads(result.stdout)
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return None


def list_monitor_devices() -> dict:
    """{"monitor:alsa_output.pci-....analog-stereo": "Internes Audio Analoges Stereo (Monitor)", ...}
    -- one entry per PipeWire sink, via `pw-dump`'s JSON (not `pactl`,
    not installed here; not `pw-cli ls Node`'s human-oriented text dump,
    which is far more annoying to parse robustly than real JSON)."""
    devices = {}
    data = _pw_dump()
    if data is None:
        return devices
    for obj in data:
        if obj.get("type") != "PipeWire:Interface:Node":
            continue
        props = obj.get("info", {}).get("props", {})
        if props.get("media.class") != "Audio/Sink":
            continue
        node_name = props.get("node.name")
        if not node_name:
            continue
        description = props.get("node.description", node_name)
        devices[f"monitor:{node_name}"] = f"{description} (Monitor)"
    return devices


def _find_persistent_node_links(data, name: str):
    """[(link_id, output_node_id, input_node_id), ...] for every link
    touching input.<name>/output.<name> in an already-parsed pw-dump."""
    node_ids = set()
    for obj in data:
        if obj.get("type") != "PipeWire:Interface:Node":
            continue
        node_name = obj.get("info", {}).get("props", {}).get("node.name", "")
        if node_name in (f"input.{name}", f"output.{name}"):
            node_ids.add(obj["id"])
    links = []
    for obj in data:
        if obj.get("type") != "PipeWire:Interface:Link":
            continue
        props = obj.get("info", {}).get("props", {})
        out_node, in_node = props.get("link.output.node"), props.get("link.input.node")
        if out_node is None or in_node is None:
            continue
        out_node, in_node = int(out_node), int(in_node)
        if out_node in node_ids or in_node in node_ids:
            links.append((obj["id"], out_node, in_node))
    return links


def ensure_persistent_input_node(name: str = _PERSISTENT_NODE_NAME):
    """Idempotent: ensures a NAMED, PERSISTENT PipeWire loopback pair
    (input.<name>/output.<name>) exists, spawning one via `pw-loopback`
    if it doesn't yet, and returns its device string in the same
    "monitor:<node.name>" shape open_input_device()/probe_device()
    already understand (this is just another node to PIPEWIRE_NODE-
    target -- see the module docstring's sink-monitor section, the
    mechanism is identical). Returns None (never raises) if anything
    here isn't available -- pw-loopback/pw-dump/pw-link missing, a
    timeout waiting for the node to appear, etc.

    Why this exists: a plain audio.source() client only exists in
    PipeWire's graph while this app is actually connected, so there's
    nothing stable to pre-wire in a patchbay tool like qpwgraph. A named
    pw-loopback pair is a real, addressable node regardless of whether
    this app is currently running -- spawned detached
    (start_new_session=True, not a child of this process) so it
    survives this app disconnecting/restarting, satisfying "create on
    demand, no separate install step, persists across app restarts."

    Real gotcha found on real hardware: WirePlumber auto-links a fresh
    pw-loopback's two ends to the system's default mic/speakers within
    ~1.5s of creation (a real feedback-loop risk, unprompted). The
    "obvious" fix -- creating it with `-i node.autoconnect=false -o
    node.autoconnect=false` -- does suppress that, but ALSO breaks the
    loopback's own internal routing (verified: manually patching both
    ends then gave 0.0 RMS captured, vs. a real non-zero signal without
    that property). The working fix is to let it auto-link normally,
    then explicitly `pw-link -d` whatever links formed -- verified this
    leaves it isolated AND still functional for deliberate patching
    afterward."""
    data = _pw_dump()
    if data is None:
        return None
    target_name = f"output.{name}"
    already_exists = any(
        obj.get("type") == "PipeWire:Interface:Node"
        and obj.get("info", {}).get("props", {}).get("node.name") == target_name
        for obj in data
    )
    if not already_exists:
        try:
            subprocess.Popen(
                ["pw-loopback", "-n", name],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except OSError:
            return None
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            time.sleep(0.1)
            data = _pw_dump()
            if data is not None and any(
                obj.get("type") == "PipeWire:Interface:Node"
                and obj.get("info", {}).get("props", {}).get("node.name") == target_name
                for obj in data
            ):
                break
        else:
            return None  # never appeared -- pw-loopback likely missing/failed silently
        # WirePlumber auto-links fresh stream nodes to system defaults --
        # strip whatever formed so this stays an isolated patch point
        # until the operator (or PlutoTxFlowgraph, once selected) wires
        # it deliberately. Best-effort per link: a link that's already
        # gone by the time we get to it isn't a real failure.
        for link_id, _out, _in in _find_persistent_node_links(data, name):
            try:
                subprocess.run(["pw-link", "-d", str(link_id)], capture_output=True, timeout=2.0)
            except (OSError, subprocess.TimeoutExpired):
                pass
    return f"monitor:{target_name}"


def _open(ctor, sample_rate: int, device_str: str):
    """Shared by open_input_device()/open_output_device(): resolves this
    module's own "monitor:<node.name>" convention (see module docstring)
    to a PIPEWIRE_NODE-targeted "pipewire" PCM open, narrowly scoped
    (saved/restored in a finally) around just this one call -- construction
    in this app is synchronous/single-threaded, so no other PIPEWIRE_NODE-
    sensitive open can race with it. Any other device string (a real
    plughw:.../hw:... ALSA name, or "" for system default) opens exactly
    as before this monitor feature existed."""
    if device_str.startswith("monitor:"):
        node_name = device_str[len("monitor:"):]
        had_prev = "PIPEWIRE_NODE" in os.environ
        prev = os.environ.get("PIPEWIRE_NODE")
        os.environ["PIPEWIRE_NODE"] = node_name
        try:
            return ctor(sample_rate, "pipewire", True)
        finally:
            if had_prev:
                os.environ["PIPEWIRE_NODE"] = prev
            else:
                os.environ.pop("PIPEWIRE_NODE", None)
    return ctor(sample_rate, device_str, True)


def open_input_device(sample_rate: int, device_str: str):
    """audio.source(), transparently handling "monitor:..." device
    strings alongside real ALSA device names/"" -- see _open()."""
    return _open(_gr_audio.source, sample_rate, device_str)


def open_output_device(sample_rate: int, device_str: str):
    """audio.sink(), same handling as open_input_device()."""
    return _open(_gr_audio.sink, sample_rate, device_str)


def probe_device(kind: str, device_str: str, sample_rate: int = 48_000):
    """Briefly open (and immediately release) an input ("input") or
    output ("output") audio device -- via open_input_device()/
    open_output_device() above, so a probed-and-approved device
    (including a "monitor:..." one) is guaranteed to open the identical
    way for real -- to check it's actually usable right now, returning
    the caught Exception on failure or None on success.

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
    opener = open_input_device if kind == "input" else open_output_device
    try:
        dev = opener(sample_rate, device_str)
        del dev
    except Exception as e:
        return e
    return None
