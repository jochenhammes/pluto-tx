"""AIOC (All-In-One-Cable) TX backend: keys a real analog radio (e.g. a Quansheng
UV-K5) connected via an AIOC adapter -- serial DTR/RTS for PTT, a USB ALSA playback
device for audio. Originally a standalone script (tx-stdin.py, still usable on its
own for piping arbitrary audio/TTS into the radio without this app); the PTT
sequencing itself lives in aioc_ptt.py, shared by both.

Audio-only like SoundcardDevice (no RF/frequency concept at all -- build_sink()
returns a null_sink, the real signal comes from a dedicated audio-output branch in
flowgraph.py, see devices/soundcard.py's module docstring for the pattern this
mirrors). UNLIKE SoundcardDevice, this device has real PTT hardware to key --
needs_ptt_control=True is what tells PlutoTxFlowgraph's is_audio_only() early-return
branches to still call pre_key()/post_unkey() here (see base.py's docstring on that
flag).

The AIOC exposes two separate USB interfaces (serial CDC-ACM for PTT, USB audio
class for the analog audio) that TxDevice's single opaque `connection` string can't
carry as-is, so this device encodes both as "<serial port>|<ALSA device>", e.g.
"/dev/ttyACM0|plughw:CARD=AllInOneCable,DEV=0" -- the same two defaults tx-stdin.py
already used. connection_kind="aioc" (a new value; gui.py renders its own label/
tooltip for it, see _update_device_connection_labels())."""
import serial
import serial.tools.list_ports
from gnuradio import gr, blocks

from .. import aioc_ptt
from .. import audio_devices
from .base import PowerStage, TxDevice

DEFAULT_SERIAL_PORT = "/dev/ttyACM0"
DEFAULT_ALSA_DEVICE = "plughw:CARD=AllInOneCable,DEV=0"

# tx-stdin.py's own defaults, carried over unchanged -- not yet calibrated
# specifically against a UV-K5 beyond the operator's own prior standalone-script use
# (see the AIOC integration plan's "Risiken/offen").
LEAD_IN_S = 0.3
TAIL_OUT_S = 0.2


def _parse_connection(connection: str):
    """"<serial>|<alsa>" -> (serial_port, alsa_device), applying the AIOC defaults
    for whichever side is missing. A bare string with no "|" is treated as just the
    serial port (the value that actually varies across machines/adapters -- the ALSA
    card name stays "AllInOneCable" for any stock AIOC)."""
    connection = (connection or "").strip()
    if not connection:
        return DEFAULT_SERIAL_PORT, DEFAULT_ALSA_DEVICE
    if "|" not in connection:
        return connection, DEFAULT_ALSA_DEVICE
    port, _, alsa = connection.partition("|")
    return port.strip() or DEFAULT_SERIAL_PORT, alsa.strip() or DEFAULT_ALSA_DEVICE


class AiocDevice(TxDevice):
    device_type = "aioc"
    display_name = "AIOC → Analogue TX"
    connection_kind = "aioc"
    DEFAULT_CONNECTION = f"{DEFAULT_SERIAL_PORT}|{DEFAULT_ALSA_DEVICE}"

    frequency_range_hz = (0.0, 0.0)  # no RF -- see TxDevice.is_audio_only()
    sample_rate_range_hz = (2_500_000, 2_500_000)
    default_sample_rate_hz = 2_500_000  # only sizes the (unused, null-sinked) FM/SSB/... resamplers
    default_bandwidth_hz = None
    supports_frequency_correction = False  # no LO
    needs_ptt_control = True  # real serial DTR/RTS PTT -- see module docstring
    # Single no-op stage, not an empty tuple -- see SoundcardDevice.power_stages for
    # why this has to exist even though the RF power path is skipped entirely.
    power_stages = (
        PowerStage(
            name="none", label="N/A", kind="continuous_db",
            min_value=0.0, max_value=0.0, unit="", is_primary=True, off_value=0.0,
        ),
    )
    default_power_ceiling = 0.0

    def __init__(self, connection, frequency_hz, sample_rate_hz, bandwidth_hz):
        super().__init__(connection, frequency_hz, sample_rate_hz, bandwidth_hz)
        self._serial_port, self._alsa_device = _parse_connection(connection)
        self._ptt = None

    def prepare_for_start(self):
        # Opened once and kept open for the whole session (across many key/unkey
        # cycles) -- the AIOC's USB CDC-ACM stack drops DTR/RTS the instant the port
        # closes, so re-opening per transmission would un-key immediately after
        # every burst. Mirrors PlutoSafety's persistent-context pattern.
        self._ptt = aioc_ptt.AiocPtt(self._serial_port, lead_in_s=LEAD_IN_S, tail_out_s=TAIL_OUT_S)

    def build_sink(self):
        return blocks.null_sink(gr.sizeof_gr_complex)

    def set_frequency(self, freq_hz):
        """No-op -- no RF to tune. Still stored so generic callers reading
        self.frequency_hz don't need a special case."""
        self.frequency_hz = freq_hz

    def set_power(self, stage_name, value):
        pass

    @property
    def audio_sink_device(self) -> str:
        return self._alsa_device

    def pre_key(self):
        self._ptt.key()

    def post_unkey(self):
        self._ptt.unkey()

    def force_safe_state(self):
        # Immediate, non-blocking, never-raising -- E-STOP/shutdown_safe() territory.
        # The serial port itself stays open (see prepare_for_start()): Re-arm after
        # E-STOP must still be able to key PTT again without a full reconnect, same
        # as Pluto's LO/HackRF's gain being safe to raise again after force_safe_state().
        if self._ptt is not None:
            self._ptt.force_release()

    def read_hw_state(self) -> dict:
        return {}

    @staticmethod
    def probe_with_timeout(connection, timeout_s=5.0):
        """Briefly open+close the serial port (an unreachable/already-claimed
        /dev/ttyACM* fails fast, no USB round trip needing a real timeout) and probe
        the ALSA output side via audio_devices.probe_device() -- same "probe before
        touching the existing flowgraph" discipline as SoundcardDevice.
        probe_with_timeout(), extended to the serial side."""
        port, alsa_device = _parse_connection(connection)
        try:
            ser = serial.Serial(port)
            ser.close()
        except Exception as e:
            return e
        return audio_devices.probe_device("output", alsa_device)

    @staticmethod
    def scan_devices_with_timeout(timeout_s=5.0):
        """Best-effort AIOC auto-detection: pairs a serial port whose USB
        description mentions the AIOC with an ALSA card named "AllInOneCable". Only
        confidently pairs them 1:1 when exactly one of each is found -- with more
        than one AIOC attached, or a differently-named third-party clone, this falls
        back to listing the raw candidates for the operator to combine by hand into
        the "<serial>|<alsa>" connection string. Verified against real AIOC hardware
        in the integration plan's hardware-test phase; device-name matching may need
        adjusting once that happens."""
        candidates = {}
        try:
            ports = serial.tools.list_ports.comports()
        except Exception:
            ports = []
        aioc_ports = [
            p for p in ports
            if any("aioc" in (getattr(p, field, "") or "").lower()
                   or "all-in-one-cable" in (getattr(p, field, "") or "").lower()
                   for field in ("description", "product", "manufacturer"))
        ]
        alsa_devices = audio_devices.list_output_devices()
        aioc_alsa = {dev: label for dev, label in alsa_devices.items() if "allinonecable" in dev.lower()}
        if len(aioc_ports) == 1 and len(aioc_alsa) == 1:
            port = aioc_ports[0].device
            alsa_device = next(iter(aioc_alsa))
            candidates[f"{port}|{alsa_device}"] = f"AIOC ({port} ↔ {aioc_alsa[alsa_device]})"
        else:
            for p in aioc_ports:
                candidates[f"{p.device}|{DEFAULT_ALSA_DEVICE}"] = f"AIOC serial: {p.device} ({p.description})"
            for dev, label in aioc_alsa.items():
                candidates[f"{DEFAULT_SERIAL_PORT}|{dev}"] = f"AIOC audio: {label}"
        return candidates, None
