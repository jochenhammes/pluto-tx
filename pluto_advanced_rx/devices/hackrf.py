"""HackRF One RX backend, via gr-soapy (gnuradio.soapy.source, driver=hackrf).

Real-hardware-verified this session (Group 1 plan, Phase C), via
get_gain_range()/has_gain_mode()/has_dc_offset_mode()/has_iq_balance_mode()/
get_sample_rate_range() on an attached HackRF One: three independent, purely
manual gain stages -- LNA (0-40dB, 8dB step), AMP (0/14dB, effectively
boolean, 14dB step so only those two values are meaningful), VGA (0-62dB,
2dB step). has_gain_mode(0) is False -- no AGC on the RX side either, same
as HackRF's TX side (pluto_tx/devices/hackrf.py). has_dc_offset_mode/
has_iq_balance_mode/has_frequency_correction are all False too -- no
correction capability exposed by this driver at all (unlike the TX side,
which needed a manual frequency-correction workaround for a real measured
offset; RX hasn't shown that symptom in testing, so it stays unimplemented
here until it does).

Sample rate is DISCRETE, not a continuous range: SoapyHackRF only accepts
whole-MHz values from 1 to 20 Msps (confirmed: get_sample_rate_range(0)
returns twenty separate (n*1e6, n*1e6, 0.0) entries, not one wide range).
sample_rate_hz_choices is a curated subset of that grid, same treatment as
Pluto's RX_BANDWIDTH_PRESETS -- kept to <=10 Msps since the IF decimation
stage downstream (flowgraph.py's rational_resampler_ccf, auto-designed taps)
hits excessive tap counts at very high decimation ratios (see
pluto_advanced_rx/config.py's RX_BANDWIDTH_PRESETS docstring for the
15M/20M finding already made on Pluto -- the same mechanism applies here).
"""
from gnuradio import soapy

from .base import GainStage, RxDevice

FREQUENCY_RANGE_HZ = (1_000_000, 7_250_000_000)
SAMPLE_RATE_HZ_CHOICES = (1_000_000, 2_000_000, 4_000_000, 5_000_000, 8_000_000, 10_000_000)
DEFAULT_SAMPLE_RATE_HZ = 4_000_000

# Reasonable starting points, not silence-guaranteeing minimums like the TX
# side's build_sink() -- RX has no radiation risk to be conservative about,
# and starting every stage at 0 would just be inaudible until the operator
# raises it. Not calibrated against any particular antenna/band; the
# operator is expected to adjust from here.
DEFAULT_LNA_GAIN_DB = 16.0
DEFAULT_VGA_GAIN_DB = 16.0


class HackRFDevice(RxDevice):
    device_type = "hackrf"
    display_name = "HackRF One"
    connection_kind = "soapy_args"
    DEFAULT_CONNECTION = ""  # empty dev_args -- Soapy picks the only attached HackRF

    frequency_range_hz = FREQUENCY_RANGE_HZ
    sample_rate_hz_choices = SAMPLE_RATE_HZ_CHOICES
    default_sample_rate_hz = DEFAULT_SAMPLE_RATE_HZ
    default_bandwidth_hz = None  # no explicit control needed, matches Pluto's "Auto" treatment
    gain_stages = (
        GainStage(name="LNA", label="LNA Gain", kind="continuous_db",
                  min_value=0.0, max_value=40.0, step=8.0, unit="dB",
                  default_value=DEFAULT_LNA_GAIN_DB, controls_agc=False),
        GainStage(name="AMP", label="RF Amp (+14dB)", kind="bool",
                  min_value=0.0, max_value=1.0, step=0.0, unit="",
                  default_value=False, controls_agc=False),
        GainStage(name="VGA", label="VGA Gain", kind="continuous_db",
                  min_value=0.0, max_value=62.0, step=2.0, unit="dB",
                  default_value=DEFAULT_VGA_GAIN_DB, controls_agc=False),
    )
    supports_agc_mode = False  # confirmed: has_gain_mode(0) is False
    agc_modes = ()
    default_gain_mode = None
    supports_dc_iq_correction = False  # confirmed: has_dc_offset_mode/has_iq_balance_mode both False

    def __init__(self, connection, frequency_hz, sample_rate_hz, bandwidth_hz):
        super().__init__(connection, frequency_hz, sample_rate_hz, bandwidth_hz)
        self._source = None

    def _device_arg(self):
        return f"driver=hackrf,serial={self.connection}" if self.connection else "driver=hackrf"

    def build_source(self):
        self._source = soapy.source(self._device_arg(), "fc32", 1, "", "", [""], [""])
        self._source.set_sample_rate(0, self.sample_rate_hz)
        if self.bandwidth_hz:
            self._source.set_bandwidth(0, self.bandwidth_hz)
        self._source.set_frequency(0, int(self.frequency_hz))
        self._source.set_gain(0, "LNA", DEFAULT_LNA_GAIN_DB)
        self._source.set_gain(0, "AMP", 0.0)
        self._source.set_gain(0, "VGA", DEFAULT_VGA_GAIN_DB)
        return self._source

    def set_frequency(self, freq_hz):
        self.frequency_hz = freq_hz
        self._source.set_frequency(0, int(freq_hz))

    def set_gain(self, stage_name, value):
        assert stage_name in {"LNA", "AMP", "VGA"}, f"HackRFDevice has no gain stage {stage_name!r}"
        if stage_name == "AMP":
            value = 14.0 if value else 0.0
        self._source.set_gain(0, stage_name, float(value))

    def read_hw_state(self) -> dict:
        if self._source is None:
            return {}
        return {
            "lna_gain_db": self._source.get_gain(0, "LNA"),
            "amp_on": self._source.get_gain(0, "AMP") > 0,
            "vga_gain_db": self._source.get_gain(0, "VGA"),
        }

    @staticmethod
    def probe_with_timeout(connection, timeout_s=5.0):
        """Constructing the source itself already fails fast if no HackRF is
        attached (or the given serial doesn't match) -- no separate raw-Soapy
        probe needed the way Pluto's libiio-over-network case needs a bounded
        thread (USB enumeration doesn't hang the way a bad network address
        can), same reasoning as pluto_tx/devices/hackrf.py's TX-side probe."""
        try:
            device_arg = f"driver=hackrf,serial={connection}" if connection else "driver=hackrf"
            soapy.source(device_arg, "fc32", 1, "", "", [""], [""])
        except Exception as e:
            return e
        return None

    @staticmethod
    def scan_devices_with_timeout(timeout_s=5.0):
        """Structured enumeration via the raw SoapySDR Python bindings
        (python3-soapysdr) -- a separate package from gr-soapy's own
        gnuradio.soapy module. Returns ({serial: label}, None) on success, or
        (None, exception) if python3-soapysdr isn't installed or the scan
        fails."""
        try:
            import SoapySDR
        except ImportError as e:
            err = RuntimeError(
                "python3-soapysdr not installed -- needed for HackRF device scanning "
                "(see install.sh). Enter a serial manually, or leave blank for the only "
                "attached HackRF."
            )
            err.__cause__ = e
            return None, err
        try:
            results = SoapySDR.Device.enumerate("driver=hackrf")
        except Exception as e:
            return None, e
        devices = {}
        for args in results:
            d = args.asdict()  # SoapySDRKwargs, not a real dict -- no .get()
            serial = d.get("serial", "")
            label = d.get("label", serial or "HackRF One")
            devices[serial] = label
        return devices, None
