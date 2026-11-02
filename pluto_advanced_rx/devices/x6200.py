"""Xiegu X6200 with the OpenX6X00 firmware as an RX device: the audio of its USB sound card, tuned over Hamlib CAT.

Audio only as far as the flowgraph is concerned (the sound card's real signal, no IQ, 48 kHz), with a frequency of its own,
so the flowgraph's audio rotator stays at 0 Hz. Measured (docs/x6200/TESTS.md): the USB output does not follow the AF volume,
and at PipeWire's 100 % capture volume its noise already clips.

connection as on the TX side: "<rig>|<audio>", with <audio> an ALSA device or "monitor:<PipeWire source node>"."""
from pluto_tx.devices.x6200 import CI_V_ADDRESS, PIPEWIRE_PREFIX, _scan_serial_ports, _set_pipewire_volume, audio_next_to, civ_id, parse_connection, resolve_rig_and_audio, rigctld_address
from pluto_tx.rigctl import DEFAULT_TCP_PORT, RigctlClient, RigctldManager, RigError

from .audio import AudioDevice, _AudioToComplexSource

CAPTURE_VOLUME = 0.5  # PipeWire's cube law: about -18 dB; see TESTS.md


class X6200RxDevice(AudioDevice):
    device_type = "x6200"
    display_name = "Xiegu X6200 (USB-Audio + CAT)"
    connection_kind = "rig"
    frequency_range_hz = (100_000.0, 54_000_000.0)
    audio_tuning_range_hz = None
    # USB only so far: nothing here switches the radio's sideband yet, and it would need NFM for FM, POCSAG and APRS.
    supported_rx_modes = frozenset({"ssb", "rade", "psk31", "rtty", "ft8", "js8", "jtty"})

    def __init__(self, connection, frequency_hz, sample_rate_hz, bandwidth_hz, buffer_size=None):
        super().__init__(connection, frequency_hz, sample_rate_hz, bandwidth_hz, buffer_size)
        rig, audio, self._model = parse_connection(connection)
        self._rig_spec, self._audio = resolve_rig_and_audio(rig, audio, "Audio/Source")
        self._client = None
        self._manager = None

    @classmethod
    def is_audio_only(cls):
        return True

    @classmethod
    def has_frequency_control(cls):
        return True

    def build_source(self):
        address = rigctld_address(self._rig_spec)
        if address:
            host, port = address
        else:
            host, port = "127.0.0.1", DEFAULT_TCP_PORT
            self._manager = RigctldManager(self._rig_spec, self._model, port)
            self._manager.ensure_running()
        self._client = RigctlClient(host, port)
        self._client.connect()
        self.set_frequency(self.frequency_hz)
        if self._audio.startswith(PIPEWIRE_PREFIX):
            _set_pipewire_volume(self._audio[len(PIPEWIRE_PREFIX):], CAPTURE_VOLUME)
        self._source = _AudioToComplexSource(self.sample_rate_hz, self._audio)
        return self._source

    def set_frequency(self, freq_hz):
        self.frequency_hz = freq_hz
        lo, hi = self.frequency_range_hz
        if self._client is not None and freq_hz and lo <= freq_hz <= hi:
            self._client.set_frequency(freq_hz)

    def read_hw_state(self):
        if self._client is None:
            return {}
        try:
            return {"frequency_hz": self._client.get_frequency(), "mode": self._client.get_mode(), "s_meter_db": self._client.get_level("STRENGTH")}
        except RigError as e:
            return {"error": str(e)}

    def close(self):
        if self._client is not None:
            self._client.close()
        self._manager = None  # rigctld keeps running: a transmitter may be using it, see RigctldManager

    @staticmethod
    def probe_with_timeout(connection, timeout_s=5.0):
        rig, _audio, _model = parse_connection(connection)
        address = rigctld_address(rig)
        client = RigctlClient(*(address or ("127.0.0.1", DEFAULT_TCP_PORT)), timeout=1.0)
        try:
            client.get_frequency()
            return None
        except RigError as e:
            if address:
                return e
        finally:
            client.close()
        if rig and civ_id(rig) != CI_V_ADDRESS:
            return RigError(f"no X6200 answers on {rig}")
        if not rig and not _scan_serial_ports():
            return RigError("no X6200 found on a serial port")
        return None

    @staticmethod
    def scan_devices_with_timeout(timeout_s=5.0):
        candidates = {}
        for port in _scan_serial_ports():
            candidates[f"{port}|{audio_next_to(port, 'Audio/Source')}"] = f"Xiegu X6200 ({port})"
        return candidates, None
