"""RTL-SDR RX backend, via gr-soapy (gnuradio.soapy.source, driver=rtlsdr).

Real-hardware-verified this session (Group 1 plan, Phase D), via
list_gains()/get_gain_range()/has_gain_mode()/get_sample_rate_range()/
get_frequency_range() on an attached RTL2838+R820T dongle: one gain stage,
"TUNER" (0-49.6dB, step reported as 0/continuous by the driver -- the R820T
actually has 29 discrete gain steps, but SoapyRtlSdr's own range interface
presents it as continuous, so it's treated that way here too). has_gain_mode
(0) is True -- REAL hardware AGC, unlike HackRF's RX side (confirmed False
there, see hackrf.py) -- set_gain_mode(0, True/False) toggles it;
RxDevice's string-based agc_modes ("manual"/"agc") maps onto that boolean at
the call boundary. has_dc_offset_mode/has_iq_balance_mode are both False (no
correction capability exposed). has_frequency_correction IS True here
(unlike HackRF) but deliberately not wired up -- out of scope for this
phase, nothing in this project has needed it on the RTL-SDR side yet.

Frequency range: confirmed via get_frequency_range(0) as ~24-1764 MHz
(R820T tuner limits).

Sample rate: SoapyRtlSdr reports two DISJOINT ranges -- confirmed via
get_sample_rate_range(0) returning [(225001, 300000), (900001, 3200000)],
the RTL2832U ADC's known "gap" between them. Rather than build a
range-with-gap concept into the GUI, sample_rate_hz_choices is (like
Pluto's RX_BANDWIDTH_PRESETS and HackRF's own curated list) a flat,
curated subset of the upper range; the 225-300kHz range is omitted
(unusual, low practical value for this app).
"""
from gnuradio import soapy

from .. import rtl_tcp
from .base import GainStage, RxDevice

FREQUENCY_RANGE_HZ = (24_000_000, 1_764_000_000)
SAMPLE_RATE_HZ_CHOICES = (
    960_000, 1_024_000, 1_200_000, 1_400_000, 1_800_000,
    1_920_000, 2_048_000, 2_400_000, 2_560_000, 3_200_000,
)
DEFAULT_SAMPLE_RATE_HZ = 2_400_000

# A middling, non-saturating starting point -- not calibrated against any
# particular antenna/band, same spirit as HackRF's DEFAULT_LNA/VGA_GAIN_DB.
DEFAULT_TUNER_GAIN_DB = 20.0

# Direct sampling: the RTL2832U's 28.8 MHz ADC samples the antenna input without the
# tuner -> HF. The first Nyquist zone ends at 14.4 MHz; the DDC still tunes up to the
# 28.8 MHz sample clock, but above 14.4 MHz every signal is ALIASED: what is tuned there is
# a mix of the wanted band and the mirror image of 28.8 MHz - f (measured: the spectrum at
# 21.0 MHz correlates +0.72 with the mirrored spectrum at 7.8 MHz) -- a band-pass in front
# of the dongle is advisable.
DIRECT_SAMPLING_NYQUIST_HZ = 14_400_000
DIRECT_SAMPLING_RANGE_HZ = (100_000, 28_800_000)


class RtlSdrDevice(RxDevice):
    device_type = "rtlsdr"
    display_name = "RTL-SDR"
    connection_kind = "soapy_args"
    DEFAULT_CONNECTION = ""  # empty dev_args -- Soapy picks the only attached RTL-SDR

    frequency_range_hz = FREQUENCY_RANGE_HZ
    sample_rate_hz_choices = SAMPLE_RATE_HZ_CHOICES
    default_sample_rate_hz = DEFAULT_SAMPLE_RATE_HZ
    default_bandwidth_hz = None  # no explicit control needed, matches Pluto/HackRF's "Auto" treatment
    gain_stages = (
        GainStage(name="TUNER", label="Tuner Gain", kind="continuous_db",
                  min_value=0.0, max_value=49.6, step=0.0, unit="dB",
                  default_value=DEFAULT_TUNER_GAIN_DB, controls_agc=True),
    )
    supports_agc_mode = True  # confirmed: has_gain_mode(0) is True
    agc_modes = ("manual", "agc")
    default_gain_mode = "manual"
    supports_dc_iq_correction = False  # confirmed: has_dc_offset_mode/has_iq_balance_mode both False
    supports_direct_sampling = True
    direct_sampling_range_hz = DIRECT_SAMPLING_RANGE_HZ

    def __init__(self, connection, frequency_hz, sample_rate_hz, bandwidth_hz, buffer_size=None):
        super().__init__(connection, frequency_hz, sample_rate_hz, bandwidth_hz, buffer_size)
        self._source = None
        # (host, port) when `connection` names an rtl_tcp server (see rtl_tcp.parse_connection),
        # None for a local USB dongle. A malformed network form raises ValueError here.
        self._tcp = rtl_tcp.parse_connection(connection)
        self._agc = False
        self._tuner_gain_db = DEFAULT_TUNER_GAIN_DB
        self._direct_sampling = 0

    def _device_arg(self):
        return f"driver=rtlsdr,serial={self.connection}" if self.connection else "driver=rtlsdr"

    def build_source(self):
        if self._tcp is not None:
            # remote dongle behind a standard rtl_tcp server -- no Soapy involved
            self._source = rtl_tcp.RtlTcpSource(*self._tcp, sample_rate_hz=self.sample_rate_hz)
            self._source.set_direct_sampling(self._direct_sampling)
            self._source.set_frequency(int(self._hw_frequency(self.frequency_hz)))
            self._source.set_gain_mode(False)
            self._source.set_gain(DEFAULT_TUNER_GAIN_DB)
            return self._source
        self._source = soapy.source(self._device_arg(), "fc32", 1, "", "", [""], [""])
        self._source.set_sample_rate(0, self.sample_rate_hz)
        if self.bandwidth_hz:
            self._source.set_bandwidth(0, self.bandwidth_hz)
        if self._direct_sampling:
            self._source.write_setting("direct_samp", str(self._direct_sampling))
        self._source.set_frequency(0, int(self._hw_frequency(self.frequency_hz)))
        self._source.set_gain_mode(0, False)  # manual by default -- caller (flowgraph.py) overrides right after
        self._source.set_gain(0, "TUNER", DEFAULT_TUNER_GAIN_DB)
        return self._source

    def set_frequency(self, freq_hz):
        self.frequency_hz = freq_hz
        self._apply_frequency()

    def _apply_frequency(self):
        if self._source is None:
            return
        hw = int(self._hw_frequency(self.frequency_hz))
        if self._tcp is not None:
            self._source.set_frequency(hw)
        else:
            self._source.set_frequency(0, hw)

    def set_direct_sampling(self, mode):
        """0 = off, 1 = I branch, 2 = Q branch (SoapyRTLSDR `direct_samp` / rtl_tcp cmd 0x09).
        Re-applies the frequency afterwards: the tuning path changes with the mode."""
        self._direct_sampling = int(mode)
        if self._source is not None:
            if self._tcp is not None:
                self._source.set_direct_sampling(self._direct_sampling)
            else:
                self._source.write_setting("direct_samp", str(self._direct_sampling))
            self._apply_frequency()

    def set_gain(self, stage_name, value):
        assert stage_name == "TUNER", f"RtlSdrDevice has no gain stage {stage_name!r}"
        self._tuner_gain_db = float(value)
        if self._tcp is not None:
            self._source.set_gain(float(value))
        else:
            self._source.set_gain(0, "TUNER", float(value))

    def set_gain_mode(self, mode):
        self._agc = mode == "agc"
        if self._tcp is not None:
            self._source.set_gain_mode(self._agc)
            if not self._agc:
                self._source.set_gain(self._tuner_gain_db)  # AGC off: re-assert the manual gain
        else:
            self._source.set_gain_mode(0, self._agc)

    def close(self):
        if self._tcp is not None and self._source is not None:
            self._source.client.close()

    def connection_status(self):
        if self._tcp is None or self._source is None:
            return None
        return self._source.status

    def read_hw_state(self) -> dict:
        if self._source is None:
            return {}
        if self._tcp is not None:
            client = self._source.client
            return {
                "rtl_tcp": f"{self._tcp[0]}:{self._tcp[1]} {client.status}",
                "net_buffer_s": f"{client.buffered_s:.2f}/{client.target_s:.2f}",
                "underruns": client.underruns,
                "tuner": rtl_tcp.TUNER_NAMES.get(client.tuner_type, client.tuner_type),
                "tuner_gain_db": self._tuner_gain_db,
                "agc_enabled": self._agc,
                "direct_sampling": self._direct_sampling,
                "freq_correction_ppm": self._frequency_correction_ppm,
                "dropped_bytes": client.dropped_bytes,
            }
        return {
            "tuner_gain_db": self._source.get_gain(0, "TUNER"),
            "agc_enabled": self._source.get_gain_mode(0),
            "direct_sampling": self._direct_sampling,
            "freq_correction_ppm": self._frequency_correction_ppm,
        }

    @staticmethod
    def probe_with_timeout(connection, timeout_s=5.0):
        """Constructing the source itself already fails fast if no RTL-SDR is
        attached (or the given serial doesn't match) -- same reasoning as
        HackRFDevice.probe_with_timeout()."""
        try:
            tcp = rtl_tcp.parse_connection(connection)
        except ValueError as e:
            return e
        if tcp is not None:
            return rtl_tcp.probe(*tcp, timeout_s=timeout_s)
        try:
            device_arg = f"driver=rtlsdr,serial={connection}" if connection else "driver=rtlsdr"
            soapy.source(device_arg, "fc32", 1, "", "", [""], [""])
        except Exception as e:
            return e
        return None

    @staticmethod
    def scan_devices_with_timeout(timeout_s=5.0):
        """Structured enumeration via the raw SoapySDR Python bindings
        (python3-soapysdr) -- same approach as HackRFDevice."""
        try:
            import SoapySDR
        except ImportError as e:
            err = RuntimeError(
                "python3-soapysdr not installed -- needed for RTL-SDR device scanning "
                "(see install.sh). Enter a serial manually, or leave blank for the only "
                "attached RTL-SDR."
            )
            err.__cause__ = e
            return None, err
        try:
            results = SoapySDR.Device.enumerate("driver=rtlsdr")
        except Exception as e:
            return None, e
        devices = {}
        for args in results:
            d = args.asdict()  # SoapySDRKwargs, not a real dict -- no .get()
            serial = d.get("serial", "")
            label = d.get("label", serial or "RTL-SDR")
            devices[serial] = label
        return devices, None
