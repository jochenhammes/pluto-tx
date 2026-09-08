"""PlutoSDR RX backend: gr-iio (fmcomms2_source_fc32). No safety layer needed
here (unlike pluto_tx/devices/pluto.py's PlutoSafety) -- an RX-only AD9361
channel can't radiate, so none of the LO-powerdown/attenuation-ceiling
machinery on the TX side applies.

Behavior-preserving move: every call here is the exact code that lived
directly in flowgraph.py before the device abstraction layer existed -- only
the object it's a method of changed. probe_with_timeout()/
scan_devices_with_timeout() delegate to pluto_tx.netutil, the one deliberate
cross-app exception this codebase already relies on (see
pluto_advanced_rx/config.py's module docstring).
"""
from gnuradio import iio

from pluto_tx import netutil

from .base import GainStage, RxDevice
from .. import config


class PlutoDevice(RxDevice):
    device_type = "pluto"
    display_name = "PlutoSDR"
    connection_kind = "uri"
    DEFAULT_CONNECTION = config.DEFAULT_URI

    frequency_range_hz = (70_000_000, 6_000_000_000)  # matches gui.py's existing freq_spin range
    # Doubles as the waterfall's "zoom levels" -- see config.py's own
    # RX_BANDWIDTH_PRESETS docstring for why this is a flat, curated list
    # rather than a continuous range (fixed IF decimation math, real
    # hardware/network throughput ceiling, scheduler limits at 15M/20M).
    sample_rate_hz_choices = tuple(config.RX_BANDWIDTH_PRESETS)
    default_sample_rate_hz = config.DEFAULT_RX_BANDWIDTH
    default_bandwidth_hz = None  # "Auto" filter mode, no explicit control (matches today's app)
    gain_stages = (
        GainStage(
            name="gain", label="RF Gain", kind="continuous_db",
            min_value=config.MANUAL_GAIN_RANGE_DB[0], max_value=config.MANUAL_GAIN_RANGE_DB[1],
            step=1.0, unit="dB", default_value=config.DEFAULT_MANUAL_GAIN_DB, controls_agc=True,
        ),
    )
    supports_agc_mode = True
    agc_modes = tuple(config.GAIN_MODES)
    default_gain_mode = config.DEFAULT_GAIN_MODE
    supports_dc_iq_correction = True

    def __init__(self, connection, frequency_hz, sample_rate_hz, bandwidth_hz):
        super().__init__(connection, frequency_hz, sample_rate_hz, bandwidth_hz)
        self._source = None

    def build_source(self):
        self._source = iio.fmcomms2_source_fc32(self.connection, [True, True], 0x8000)
        self._source.set_frequency(int(self.frequency_hz))
        self._source.set_samplerate(int(self.sample_rate_hz))
        self._source.set_quadrature(True)
        self._source.set_rfdc(True)
        self._source.set_bbdc(True)
        self._source.set_filter_params("Auto", "", 0, 0)
        return self._source

    def set_frequency(self, freq_hz):
        self.frequency_hz = freq_hz
        self._source.set_frequency(int(freq_hz))

    def set_gain(self, stage_name, value):
        assert stage_name == "gain", f"PlutoDevice has no gain stage {stage_name!r}"
        self._source.set_gain(0, value)

    def set_gain_mode(self, mode):
        self._source.set_gain_mode(0, mode)

    def read_hw_state(self) -> dict:
        return {}  # nothing wired up to a GUI status readback yet, unlike the TX side

    @staticmethod
    def probe_with_timeout(connection, timeout_s=netutil.CONNECT_TIMEOUT_S):
        return netutil.probe_uri_with_timeout(connection, timeout_s)

    @staticmethod
    def scan_devices_with_timeout(timeout_s=netutil.CONNECT_TIMEOUT_S):
        return netutil.scan_devices_with_timeout(timeout_s)
