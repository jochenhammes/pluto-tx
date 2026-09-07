"""PlutoSDR TX backend: gr-iio (fmcomms2_sink_fc32) plus the raw-libiio
safety layer (PlutoSafety, safety.py) that hard-ties PTT to the AD9361 TX
LO's powerdown bit, in every mode -- kept deliberately independent of the
GNU Radio runtime (safety.py's own docstring: must keep working even if the
GR runtime crashed).

This is a behavior-preserving move: every call here is the exact code that
lived directly in flowgraph.py before the device abstraction layer existed
(verified end-to-end on real Pluto+PA hardware, all four modulation modes,
this session) -- only the object it's a method of changed. safety.py and
netutil.py are untouched; this module only imports and delegates to them.

MIN_ATTEN/MAX_ATTEN/DEFAULT_URI stay in the top-level config.py rather than
moving here too: safety.py (deliberately left untouched) already imports
them from there, and having safety.py import back from this module would
be a circular import (this module imports PlutoSafety from safety.py).
"""
import time

from gnuradio import iio

from .base import PowerStage, TxDevice
from .. import config
from .. import netutil
from ..safety import PlutoSafety

# GUI power slider default ceiling: never select less attenuation than this
# (i.e. never more power than this) unless the "unlock full power" box is checked.
DEFAULT_ATTEN_CEILING = -20.0

# PTT is hard-tied to the TX LO powerdown bit (ad9361-phy altvoltage1,
# see safety.py's power_down_lo()), in every mode -- not just at app
# shutdown. Reason: the TX attenuator alone (down to MIN_ATTEN) does not
# fully suppress LO leakage; an external PA connected to the Pluto's TX
# port amplifies that residual leakage into an audible/measurable spike
# whenever the LO is left running between transmissions. So: LO powered
# down whenever unkeyed (idle at app start, and after every unkey_ptt()/
# finish_unkey_m17()), powered back up only for the duration of key_ptt().
# LO_RELOCK_S is how long key_ptt() waits after powering the LO back up
# before actually unmuting audio and raising TX power, to let the AD9361's
# synthesizer relock first -- a conservative placeholder, NOT measured
# against real hardware (unlike M17_EOT_HOLD_S in config.py, which was
# calibrated against a real PTT release). Needs the same real-PTT
# calibration pass if transmit quality issues show up right at key-up.
LO_RELOCK_S = 0.005

# Shared TX baseband ("quadrature") rate for both FM and SSB. fmcomms2_sink
# needs set_filter_params() to go below the AD9361's hardware ADC/DAC floor
# (~2.083 MHz) -- we don't configure that, so QUAD_RATE must stay >= that
# floor. 2.5 MSps also matches the rate already verified working on real
# hardware (RX capture + the SDRangel TX test).
QUAD_RATE = 2_500_000

DEFAULT_BANDWIDTH = 200_000  # Hz, AD9361 analog TX filter (min allowed is 200000)


def _pluto_atten_arg(db: float) -> float:
    """gr-iio's fmcomms2_sink_fc32.set_attenuation() expects a POSITIVE dB
    magnitude (it negates internally before writing the AD9361 hardwaregain
    register) -- the opposite sign convention from raw libiio/iio_attr, which
    this codebase otherwise uses everywhere (config.MIN_ATTEN = -89.75, etc).
    Convert only at this boundary."""
    return abs(db)


class PlutoDevice(TxDevice):
    device_type = "pluto"
    display_name = "PlutoSDR"
    connection_kind = "uri"
    DEFAULT_CONNECTION = config.DEFAULT_URI

    frequency_range_hz = (47_000_000, 6_000_000_000)
    sample_rate_range_hz = (QUAD_RATE, QUAD_RATE)  # fixed, no runtime control (matches today's app)
    default_sample_rate_hz = QUAD_RATE
    default_bandwidth_hz = DEFAULT_BANDWIDTH
    power_stages = (
        PowerStage(
            name="attenuation", label="TX Power (Attenuation)", kind="continuous_db",
            min_value=config.MIN_ATTEN, max_value=config.MAX_ATTEN, unit="dB",
            is_primary=True, off_value=config.MIN_ATTEN,
        ),
    )
    default_power_ceiling = DEFAULT_ATTEN_CEILING

    def __init__(self, connection, frequency_hz, sample_rate_hz, bandwidth_hz):
        super().__init__(connection, frequency_hz, sample_rate_hz, bandwidth_hz)
        self._safety = PlutoSafety(connection)
        self._sink = None

    def prepare_for_start(self):
        self._safety.prepare_for_start()

    def build_sink(self):
        # ALWAYS constructed at MIN_ATTEN, never the operator's target power
        # (the constructor enables TX channels immediately, before tb.start()
        # is ever called).
        self._sink = iio.fmcomms2_sink_fc32(self.connection, [True, True], 0x8000, False)
        self._sink.set_bandwidth(int(self.bandwidth_hz))
        self._sink.set_frequency(int(self.frequency_hz))
        self._sink.set_samplerate(int(self.sample_rate_hz))
        self._sink.set_attenuation(0, _pluto_atten_arg(config.MIN_ATTEN))
        return self._sink

    def set_frequency(self, freq_hz):
        self.frequency_hz = freq_hz
        self._sink.set_frequency(int(freq_hz))

    def set_power(self, stage_name, value):
        assert stage_name == "attenuation", f"PlutoDevice has no power stage {stage_name!r}"
        self._sink.set_attenuation(0, _pluto_atten_arg(value))

    def pre_key(self):
        self._safety.power_down_lo(False)
        time.sleep(LO_RELOCK_S)

    def post_unkey(self):
        self._safety.power_down_lo(True)

    def force_safe_state(self):
        self._safety.force_safe_state()

    def read_hw_state(self) -> dict:
        return self._safety.read_state()

    @staticmethod
    def probe_with_timeout(connection, timeout_s=netutil.CONNECT_TIMEOUT_S):
        return netutil.probe_uri_with_timeout(connection, timeout_s)

    @staticmethod
    def scan_devices_with_timeout(timeout_s=netutil.CONNECT_TIMEOUT_S):
        return netutil.scan_devices_with_timeout(timeout_s)
