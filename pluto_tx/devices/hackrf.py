"""HackRF One TX backend, via gr-soapy (gnuradio.soapy.sink, driver=hackrf).

Architecturally different from PlutoDevice in one important, CONFIRMED-ON-
REAL-HARDWARE way: this system's installed gnuradio.soapy Python bindings
expose NO activate()/deactivate() or other per-stream lifecycle call
(confirmed by inspecting dir(soapy.sink) -- only the inherited gr::block
start()/stop(), not safe to call on a single block while the rest of the
flowgraph keeps running). Zeroing every gain stage this device exposes
(VGA=0, AMP=off) does NOT actually stop transmission on real hardware --
the operator confirmed the HackRF kept transmitting after a normal PTT
release AND after E-STOP, and only Disconnect (which tears the whole
flowgraph, including the sink, down) actually stopped it. So
supports_persistent_sink = False here: flowgraph.py rebuilds this device's
sink (via build_sink() again) on every unkey/E-STOP instead of just
zeroing gain on an object that stays open for the whole session -- the
same "actually closes the stream" effect Disconnect already had, just
automated per PTT cycle. pre_key()/post_unkey() themselves stay simple
(gain-only); the rebuild is flowgraph.py's job, gated by
supports_persistent_sink, since it needs tb.lock()/disconnect()/connect()/
unlock() -- machinery the device object doesn't have access to.

HackRF's TX gain model, confirmed via get_gain_range() on real hardware:
- "VGA" ("IF Gain" in the GUI): continuous, 0-47 dB, step 1 dB -- the
  primary/continuous stage.
- "AMP" ("RF Gain" in the GUI): range-typed but effectively boolean --
  min=0.0, max=14.0, step=14.0 (only those two values are meaningful;
  get_gain(0,"AMP") > 0 reliably means "on").
Both are ADDITIVE gain (larger value = more RF power) -- opposite sign
convention from Pluto's attenuation, but the same "larger = more power"
direction PowerStage's clamp logic already assumes.
"""
from gnuradio import soapy

from .base import PowerStage, TxDevice

FREQUENCY_RANGE_HZ = (1_000_000, 6_000_000_000)
SAMPLE_RATE_RANGE_HZ = (2_000_000, 20_000_000)
# 2 Msps: enough for every voice mode this app supports (FM +-2.5kHz,
# SSB, M17, FreeDV, RADE -- none needs more than ~200kHz of bandwidth) and
# gives a clean interpolation=125/decimation=3 fm_resampler_fff ratio
# against this app's 48kHz audio rate. Was 8_000_000 -- found too high on
# real hardware (an Intel i5-6300U dual-core laptop): fm_resampler_fff's
# interpolation=500 (500 polyphase branches) PLUS the TX waterfall's own
# 8Msps->50kHz resampler (a very tight 160:1 antialiasing filter running
# at the full 8Msps input rate) together overran the CPU's ability to keep
# the HackRF's USB buffer fed, producing real GNU Radio buffer underruns
# (a choppy, unintelligible received signal). The old comment's claim of
# "comparable computational cost" to Pluto's 2.5Msps was wrong: resampler
# cost scales with sample_rate * n_taps, and 8Msps/500 taps is measurably
# MORE expensive than Pluto's 625:12 ratio at 2.5Msps, on top of the
# waterfall resampler also running 3.2x faster. At 2Msps: interpolation
# drops to 125 taps, and the waterfall resampler itself runs 4x slower --
# roughly an order of magnitude less total resampler load than 8Msps.
DEFAULT_SAMPLE_RATE_HZ = 2_000_000

# Deliberately conservative, NOT a computed "equivalent" of Pluto's
# -20 dBFS default -- the two devices' gain chains and external-PA drive
# requirements aren't characterized against each other. Revisit once real-
# hardware testing (see the device-abstraction plan's Phase C) says more.
DEFAULT_VGA_CEILING_DB = 20.0


class HackRFDevice(TxDevice):
    device_type = "hackrf"
    display_name = "HackRF One"
    connection_kind = "serial"
    DEFAULT_CONNECTION = ""  # empty dev_args -- Soapy picks the only attached HackRF

    frequency_range_hz = FREQUENCY_RANGE_HZ
    sample_rate_range_hz = SAMPLE_RATE_RANGE_HZ
    default_sample_rate_hz = DEFAULT_SAMPLE_RATE_HZ
    default_bandwidth_hz = None  # "Auto" -- HackRF has no Pluto-style analog TX filter floor
    power_stages = (
        PowerStage(
            name="VGA", label="IF Gain", kind="continuous_db",
            min_value=0.0, max_value=47.0, unit="dB", is_primary=True, off_value=0.0,
        ),
        PowerStage(
            name="AMP", label="RF Gain (+14dB)", kind="bool",
            min_value=0.0, max_value=1.0, unit="", is_primary=False, off_value=False,
        ),
    )
    default_power_ceiling = DEFAULT_VGA_CEILING_DB
    supports_persistent_sink = False  # see module docstring -- confirmed on real hardware
    # No frequency-correction capability is exposed by this system's
    # SoapyHackRF driver (confirmed: has_frequency_correction(0) is False,
    # list_frequencies(0) only shows a single "RF" component, no separate
    # "CORR" tuning element) -- yet a real, consistent offset was observed
    # on a HackRF without TCXO (~56 ppm low). So the correction is a manual,
    # operator-tuned ppm value applied in software to the LO frequency (see
    # TxDevice._hw_frequency() / freq_correction.py); default 0 -- a per-unit
    # characteristic, never a built-in constant.

    def __init__(self, connection, frequency_hz, sample_rate_hz, bandwidth_hz):
        super().__init__(connection, frequency_hz, sample_rate_hz, bandwidth_hz)
        self._sink = None

    def _device_arg(self):
        return f"driver=hackrf,serial={self.connection}" if self.connection else "driver=hackrf"

    def prepare_for_start(self):
        pass  # no independent-of-GNU-Radio safety layer exists for this backend, see module docstring

    def build_sink(self):
        # ALWAYS constructed at minimum power (VGA=0, AMP off), never the
        # operator's target power -- same discipline as PlutoDevice.
        self._sink = soapy.sink(self._device_arg(), "fc32", 1, "", "", [""], [""])
        self._sink.set_sample_rate(0, self.sample_rate_hz)
        if self.bandwidth_hz:
            self._sink.set_bandwidth(0, self.bandwidth_hz)
        self._sink.set_frequency(0, int(self._hw_frequency(self.frequency_hz)))
        self._sink.set_gain(0, "VGA", 0.0)
        self._sink.set_gain(0, "AMP", 0.0)
        return self._sink

    def set_frequency(self, freq_hz):
        self.frequency_hz = freq_hz
        self._sink.set_frequency(0, int(self._hw_frequency(freq_hz)))

    def _apply_frequency(self):
        if self._sink is not None:
            self._sink.set_frequency(0, int(self._hw_frequency(self.frequency_hz)))

    def set_power(self, stage_name, value):
        if stage_name == "AMP":
            value = 14.0 if value else 0.0
        self._sink.set_gain(0, stage_name, float(value))

    def pre_key(self):
        pass  # no known relock-equivalent delay for this backend

    def post_unkey(self):
        # Gain-only -- confirmed on real hardware this alone does NOT
        # actually stop transmission (see module docstring); the real fix
        # is flowgraph.py rebuilding the sink entirely on unkey, gated by
        # supports_persistent_sink=False. Still zero AMP here too (VGA is
        # already zeroed generically by flowgraph.py's unkey_ptt() before
        # this runs) so read_hw_state() reflects "off" immediately, even in
        # the brief window before the rebuild completes.
        self._sink.set_gain(0, "AMP", 0.0)

    def force_safe_state(self):
        if self._sink is None:
            return
        try:
            self._sink.set_gain(0, "VGA", 0.0)
            self._sink.set_gain(0, "AMP", 0.0)
        except Exception:
            pass  # never raise -- see TxDevice.force_safe_state()'s contract

    def read_hw_state(self) -> dict:
        if self._sink is None:
            return {}
        return {
            "vga_gain_db": self._sink.get_gain(0, "VGA"),
            "amp_on": self._sink.get_gain(0, "AMP") > 0,
            "freq_correction_ppm": self._frequency_correction_ppm,
        }

    @staticmethod
    def probe_with_timeout(connection, timeout_s=5.0):
        """Constructing the sink itself already fails fast if no HackRF is
        attached (or the given serial doesn't match) -- no separate raw-
        Soapy probe needed the way Pluto's libiio-over-network case needs a
        bounded thread (USB enumeration doesn't hang the way a bad network
        address can)."""
        try:
            device_arg = f"driver=hackrf,serial={connection}" if connection else "driver=hackrf"
            soapy.sink(device_arg, "fc32", 1, "", "", [""], [""])
        except Exception as e:
            return e
        return None

    @staticmethod
    def scan_devices_with_timeout(timeout_s=5.0):
        """Structured enumeration via the raw SoapySDR Python bindings
        (python3-soapysdr) -- a separate package from gr-soapy's own
        gnuradio.soapy module (which needs no such binding for build_sink()
        itself). Returns ({serial: label}, None) on success, or
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
