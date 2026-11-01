"""Xiegu X6200 with the OpenX6X00 firmware as a TX device: USB sound card for the signal, Hamlib CAT for everything else.

A transceiver, so its signal path is audio like the AIOC's (is_audio_only(): every mode's audio branch in flowgraph.py) but
it has a frequency and a power of its own (has_frequency_control()). Measured on the real radio (docs/x6200/TESTS.md):
- it takes USB audio only in the data modes USB-D and LSB-D; plain USB and LSB take the microphone, AM and NFM send a bare
  carrier. Hence the mode list below, and M PKTUSB / PKTLSB before keying.
- a new PTT state reads back up to 200 ms late, so the audio stays muted until the state has been seen.
- a keyed radio stays keyed if the host dies: see rig_guard.py.

connection: "<rig>|<audio>"; <rig> is a serial port or rigctld://host:port (optionally @<hamlib model>), <audio> an ALSA
device or "monitor:<PipeWire node>", the only way in while PipeWire holds the card."""
import json
import os
import shutil
import subprocess
import sys
import threading
import time

import serial
import serial.tools.list_ports
from gnuradio import gr, blocks

from .. import config
from .. import rig_guard
from ..rigctl import DEFAULT_TCP_PORT, RigctlClient, RigctldManager, RigError
from .base import PowerStage, TxDevice

HAMLIB_MODEL = 3091
CI_V_ADDRESS = 0xA4
USB_SERIAL_ID = (0x1A86, 0x55D2)    # the radio's two serial ports; one answers CI-V
SERIAL_BAUD = 19200
PTT_SEEN_TIMEOUT_S = 0.8            # 200 ms measured
PTT_POLL_S = 0.05
POWER_TOLERANCE = 0.006             # the radio's step is 1/255
MAX_KEY_S = 120.0                   # the guard ends a transmission after this, whatever the app does
PIPEWIRE_PREFIX = "monitor:"
CIV_MODE_DATA = {"usb": "PKTUSB", "lsb": "PKTLSB"}
LOWER_SIDEBAND_MODES = ("lsb",)


def parse_connection(connection):
    """-> (rig, audio, model)."""
    rig, _, audio = (connection or "").strip().partition("|")
    rig, _, model = rig.strip().partition("@")
    return rig.strip(), audio.strip(), int(model) if model.strip() else HAMLIB_MODEL


def rigctld_address(rig):
    """"rigctld://host:port" -> (host, port), else None for a serial port."""
    if not rig.startswith("rigctld://"):
        return None
    host, _, port = rig[len("rigctld://"):].partition(":")
    return host or "127.0.0.1", int(port or DEFAULT_TCP_PORT)


def civ_id(port, timeout=0.5):
    """The CI-V transceiver ID on a serial port, or None. Opens it without DTR or RTS."""
    try:
        s = serial.Serial()
        s.port, s.baudrate, s.timeout, s.dtr, s.rts = port, SERIAL_BAUD, timeout, False, False
        s.open()
    except (OSError, serial.SerialException):
        return None
    try:
        s.write(bytes([0xFE, 0xFE, CI_V_ADDRESS, 0xE0, 0x19, 0x00, 0xFD]))
        data = s.read(32)
    except (OSError, serial.SerialException):
        return None
    finally:
        s.close()
    marker = bytes([0xFE, 0xFE, 0xE0, CI_V_ADDRESS, 0x19, 0x00])
    i = data.find(marker)
    return data[i + len(marker)] if i >= 0 and len(data) > i + len(marker) else None


def _usb_hub_path(sys_device):
    """The USB hub a sysfs device hangs off: its radio's serial ports and sound card share one."""
    path = os.path.realpath(sys_device)
    while path != "/" and not os.path.exists(os.path.join(path, "idVendor")):
        path = os.path.dirname(path)
    return os.path.dirname(path)


def pipewire_node_for_card(card, media_class):
    """PipeWire's node of an ALSA card ("Audio/Sink", "Audio/Source"), or None without PipeWire."""
    try:
        nodes = json.loads(subprocess.run(["pw-dump"], capture_output=True, text=True, timeout=3).stdout)
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    for node in nodes:
        props = (node.get("info") or {}).get("props") or {}
        if props.get("media.class") == media_class and str(props.get("alsa.card")) == str(card):
            return props.get("node.name")
    return None


def _set_pipewire_volume(node_name, value=1.0):
    """The radio's input is calibrated at full scale; PipeWire's default for a new card is 40 %, about -24 dB."""
    if shutil.which("wpctl"):
        try:
            nodes = json.loads(subprocess.run(["pw-dump"], capture_output=True, text=True, timeout=3).stdout)
            for node in nodes:
                if ((node.get("info") or {}).get("props") or {}).get("node.name") == node_name:
                    subprocess.run(["wpctl", "set-volume", str(node["id"]), str(value)], capture_output=True, timeout=3)
        except (OSError, ValueError, subprocess.SubprocessError):
            pass


class X6200Device(TxDevice):
    device_type = "x6200"
    display_name = "Xiegu X6200 (USB-Audio + CAT)"
    connection_kind = "rig"
    DEFAULT_CONNECTION = ""

    frequency_range_hz = (1_800_000.0, 54_000_000.0)
    sample_rate_range_hz = (2_500_000, 2_500_000)
    default_sample_rate_hz = 2_500_000  # only sizes the unused, null-sinked resamplers, as with the AIOC
    default_bandwidth_hz = None
    supports_frequency_correction = False
    needs_ptt_control = True
    supported_modes = frozenset({"ssb", "lsb", "rade", "digitext", "psk31", "rtty", "ft8", "js8", "jtty"})
    supported_modes_reason = "it takes USB audio only in USB-D and LSB-D, so nothing that needs FM or AM"
    power_stages = (
        PowerStage(name="rfpower", label="RF power", kind="continuous_db", min_value=0.0, max_value=100.0, unit="%",
                   is_primary=True, off_value=0.0),
    )
    default_power_ceiling = 10.0     # of 8 W, 5 W on battery
    tx_end_loss_s = 0.3              # audio still on its way through PipeWire and the radio when the source ends

    def __init__(self, connection, frequency_hz, sample_rate_hz, bandwidth_hz):
        super().__init__(connection, frequency_hz, sample_rate_hz, bandwidth_hz)
        rig, audio, self._model = parse_connection(connection)
        self._rig_spec, self._audio = resolve_rig_and_audio(rig, audio, "Audio/Sink")
        self._client = None       # request path of the app
        self._estop_client = None # its own socket, so that E-STOP never waits for a request in progress
        self._manager = None
        self._guard = None
        self._power_pct = 0.0
        self._rig_mode = "PKTUSB"
        self._keyed = False
        self._lock = threading.Lock()

    @classmethod
    def is_audio_only(cls):
        return True

    @classmethod
    def has_frequency_control(cls):
        return True

    @property
    def audio_sink_device(self):
        return self._audio

    def select_mode(self, mode_name):
        self._rig_mode = CIV_MODE_DATA["lsb" if mode_name in LOWER_SIDEBAND_MODES else "usb"]

    def prepare_for_start(self):
        address = rigctld_address(self._rig_spec)
        if address:
            host, port = address
        else:
            host, port = "127.0.0.1", DEFAULT_TCP_PORT
            self._manager = RigctldManager(self._rig_spec, self._model, port)
            self._manager.ensure_running()
        self._client = RigctlClient(host, port)
        self._estop_client = RigctlClient(host, port, timeout=0.5)
        self._client.connect()
        self._client.set_ptt(False)
        self._start_guard(port)
        if self._audio.startswith(PIPEWIRE_PREFIX):
            _set_pipewire_volume(self._audio[len(PIPEWIRE_PREFIX):])
        if self.frequency_hz and self.frequency_range_hz[0] <= self.frequency_hz <= self.frequency_range_hz[1]:
            self._client.set_frequency(self.frequency_hz)

    def _start_guard(self, port):
        command = [sys.executable, os.path.abspath(rig_guard.__file__), str(port)]
        self._guard = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)

    def _tell_guard(self, line):
        try:
            self._guard.stdin.write((line + "\n").encode())
            self._guard.stdin.flush()
        except (OSError, ValueError, AttributeError):
            pass

    def build_sink(self):
        return blocks.null_sink(gr.sizeof_gr_complex)

    def set_frequency(self, freq_hz):
        self.frequency_hz = freq_hz
        lo, hi = self.frequency_range_hz
        if self._client is not None and lo <= freq_hz <= hi and not self._keyed:
            self._client.set_frequency(freq_hz)

    def set_power(self, stage_name, value):
        self._power_pct = float(value)
        if self._client is not None:
            self._apply_power()

    def _apply_power(self):
        """The readback decides: the firmware answers a power write with the value instead of FB, which Hamlib reports as
        error -5 although the radio took it (docs/x6200/TESTS.md)."""
        wanted = self._power_pct / 100.0
        if abs(self._client.get_level("RFPOWER") - wanted) <= POWER_TOLERANCE:
            return
        try:
            self._client.set_level("RFPOWER", wanted)
        except RigError:
            pass
        actual = self._client.get_level("RFPOWER")
        if abs(actual - wanted) > POWER_TOLERANCE:
            raise RigError(f"radio stayed at {actual * 100:.0f} % power instead of {wanted * 100:.0f} %")

    def pre_key(self):
        """Refuses before any RF: the tuned frequency and the mode are read back from the radio, and the caller unmutes the
        audio only after this returned, with the radio seen transmitting."""
        lo, hi = self.frequency_range_hz
        if not lo <= self.frequency_hz <= hi:
            raise RigError(f"{self.frequency_hz / 1e6:.3f} MHz is outside the radio's range")
        self._client.set_frequency(self.frequency_hz)
        tuned = self._client.get_frequency()
        if abs(tuned - self.frequency_hz) > 1.0:
            raise RigError(f"radio stayed on {tuned / 1e6:.6f} MHz instead of {self.frequency_hz / 1e6:.6f} MHz")
        if config.in_amateur_band(tuned) is None:
            raise RigError(f"{tuned / 1e6:.3f} MHz is outside the amateur bands")
        self._client.set_mode(self._rig_mode)
        mode = self._client.get_mode()
        if mode != self._rig_mode:
            raise RigError(f"radio is in {mode}, not {self._rig_mode}: it would not take the USB audio")
        self._apply_power()
        self._tell_guard(f"key {MAX_KEY_S}")
        self._keyed = True
        try:
            self._client.set_ptt(True)
            end = time.monotonic() + PTT_SEEN_TIMEOUT_S
            while not self._client.get_ptt():
                if time.monotonic() > end:
                    raise RigError("radio refused to transmit (PTT stayed off)")
                time.sleep(PTT_POLL_S)
        except RigError:
            self._release()
            raise

    def _release(self):
        self._keyed = False
        try:
            self._client.set_ptt(False)
            end = time.monotonic() + PTT_SEEN_TIMEOUT_S
            while self._client.get_ptt() and time.monotonic() < end:
                time.sleep(PTT_POLL_S)
        finally:
            self._tell_guard("unkey")

    def post_unkey(self):
        try:
            self._release()
        except RigError:
            self.force_safe_state()

    def force_safe_state(self):
        """Idempotent, never raises, never waits for a request in progress. If the socket fails, the guard's time still runs."""
        self._keyed = False
        client = self._estop_client
        if client is None:
            return
        try:
            client.set_ptt(False)
            self._tell_guard("unkey")
        except Exception:  # noqa: BLE001 -- the last-resort path must not raise
            self._tell_guard(f"key {rig_guard.SETTLE_S}")

    def close(self):
        """Ends the guard, which also releases the PTT once more. rigctld keeps running, see RigctldManager."""
        guard, self._guard = self._guard, None
        if guard is not None:
            try:
                guard.stdin.close()
            except OSError:
                pass
            try:
                guard.wait(timeout=5.0)
            except subprocess.TimeoutExpired:
                guard.kill()
        self._manager = None

    def read_hw_state(self):
        if self._client is None:
            return {}
        state = {}
        try:
            state["frequency_hz"] = self._client.get_frequency()
            state["mode"] = self._client.get_mode()
            state["ptt"] = self._client.get_ptt()
            state["rf_power_pct"] = round(self._client.get_level("RFPOWER") * 100.0, 1)
            if state["ptt"]:
                state["swr"] = self._client.get_level("SWR")
                state["alc"] = self._client.get_level("ALC")
            else:
                state["s_meter_db"] = self._client.get_level("STRENGTH")
        except RigError as e:
            state["error"] = str(e)
        return state

    @staticmethod
    def probe_with_timeout(connection, timeout_s=5.0):
        rig, _audio, _model = parse_connection(connection)
        address = rigctld_address(rig)
        if address:
            client = RigctlClient(*address, timeout=min(timeout_s, 2.0))
            try:
                client.get_frequency()
                return None
            except RigError as e:
                return e
            finally:
                client.close()
        client = RigctlClient(port=DEFAULT_TCP_PORT, timeout=1.0)  # a rigctld already holding the port is fine
        try:
            client.get_frequency()
            return None
        except RigError:
            pass
        finally:
            client.close()
        ports = [rig] if rig else _scan_serial_ports()
        if not ports:
            return RigError("no X6200 found on a serial port")
        if rig and civ_id(rig) != CI_V_ADDRESS:
            return RigError(f"no X6200 answers on {rig}")
        return None

    @staticmethod
    def scan_devices_with_timeout(timeout_s=5.0):
        candidates = {}
        for port in _scan_serial_ports():
            audio = audio_next_to(port)
            label = f"Xiegu X6200 ({port}" + (f", {audio[len(PIPEWIRE_PREFIX):]}" if audio else ", no sound card found") + ")"
            candidates[f"{port}|{audio}"] = label
        return candidates, None


def resolve_rig_and_audio(rig, audio, media_class):
    """-> (rig, audio) with a blank part filled in from what is attached. Raises RigError rather than leave the audio blank: a
    blank device is the system default, and the radio would be keyed while the signal plays on the speakers."""
    if not rig:
        rig = next(iter(_scan_serial_ports()), "")
        if not rig:
            raise RigError("no X6200 found on a serial port")
    if not audio and not rigctld_address(rig):
        audio = audio_next_to(rig, media_class)
    if not audio:
        raise RigError(f"no sound card known for the radio on {rig}: give it after a |, as in {rig}|plughw:CARD=...")
    return rig, audio


def _scan_serial_ports():
    """The serial ports of the radio's USB ID that answer CI-V with its address. Two ports show up, one is CAT."""
    found = []
    for p in serial.tools.list_ports.comports():
        if (p.vid, p.pid) == USB_SERIAL_ID and civ_id(p.device) == CI_V_ADDRESS:
            found.append(p.device)
    return found


def audio_next_to(port, media_class="Audio/Sink"):
    """The sound card on the same USB hub as the serial port, as a PipeWire node if there is one, else an ALSA device."""
    tty = os.path.join("/sys/class/tty", os.path.basename(port), "device")
    hub = _usb_hub_path(tty)
    root = "/sys/class/sound"
    if not os.path.isdir(root):
        return ""
    for name in sorted(os.listdir(root)):
        if not (name.startswith("card") and name[4:].isdigit()):
            continue
        device = os.path.join(root, name, "device")
        if os.path.exists(device) and _usb_hub_path(device) == hub:
            node = pipewire_node_for_card(name[4:], media_class)
            if node:
                return PIPEWIRE_PREFIX + node
            with open(os.path.join(root, name, "id")) as f:
                return f"plughw:CARD={f.read().strip()},DEV=0"
    return ""
