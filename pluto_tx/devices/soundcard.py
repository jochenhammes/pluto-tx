"""Soundcard TX backend: lets an externally-connected SSB transceiver
transmit RADE via a sound card instead of this app's own SDR (Pluto/HackRF).

No RF concept applies here at all -- no frequency, no power stage, no scan.
build_sink() returns a null_sink: the RF-domain path (mode_selector -> tx_gain
-> device sink) is still constructed for this device (every modulation branch
is always connected, this app's established pattern) but carries no meaning
here and goes nowhere. The actual audio-injectable signal comes from RADE's
own dedicated soundcard-output branch in flowgraph.py (rade_encoder ->
rade_to_real -> ... -> rade_audio_sink), which taps rade_encoder directly and
has nothing to do with this device's build_sink() -- see PlutoTxFlowgraph's
RADE branch construction comment and key_ptt()/unkey_ptt()'s is_audio_only()
branch for how the two are tied together.

Mirrors pluto_advanced_rx/devices/audio.py's AudioDevice (the RX-side
equivalent added last session) wherever the TxDevice/RxDevice ABCs allow.
"""
from gnuradio import gr, blocks

from .base import PowerStage, TxDevice


class SoundcardDevice(TxDevice):
    device_type = "soundcard"
    display_name = "Soundcard (RADE only)"
    connection_kind = "audio_device"
    DEFAULT_CONNECTION = ""  # empty device string -- gr-audio picks the system default

    frequency_range_hz = (0.0, 0.0)  # no RF -- see TxDevice.is_audio_only()
    sample_rate_range_hz = (2_500_000, 2_500_000)
    default_sample_rate_hz = 2_500_000  # only sizes the (unused, null-sinked) FM/SSB/M17/FreeDV/RADE-SDR resamplers
    default_bandwidth_hz = None
    # A single no-op stage, not an empty tuple: primary_stage/
    # devices.primary_power_stage() do next(s for s in power_stages if
    # s.is_primary), which would raise StopIteration on an empty tuple --
    # several GUI code paths (power slider range, _rebuild()'s unconditional
    # set_target_power() call) read this generically regardless of device
    # type, so it must exist even though it's never meaningfully used (the
    # RF power path is skipped entirely for this device, see key_ptt()).
    power_stages = (
        PowerStage(
            name="none", label="N/A", kind="continuous_db",
            min_value=0.0, max_value=0.0, unit="", is_primary=True, off_value=0.0,
        ),
    )
    default_power_ceiling = 0.0

    def prepare_for_start(self):
        pass

    def build_sink(self):
        return blocks.null_sink(gr.sizeof_gr_complex)

    def set_frequency(self, freq_hz):
        """No-op -- no RF to tune. Still stored so generic callers reading
        self.frequency_hz don't need a special case."""
        self.frequency_hz = freq_hz

    def set_power(self, stage_name, value):
        pass

    def pre_key(self):
        pass

    def post_unkey(self):
        pass

    def force_safe_state(self):
        pass

    def read_hw_state(self) -> dict:
        return {}

    @staticmethod
    def probe_with_timeout(connection, timeout_s=5.0):
        """A sound card is treated as always available -- no equivalent of
        libiio's/SoapySDR's reachability check exists for gr-audio."""
        return None

    @staticmethod
    def scan_devices_with_timeout(timeout_s=5.0):
        """No structured device enumeration exists for gnuradio.audio -- the
        GUI's existing "0 devices found" handling covers this fine."""
        return {}, None
