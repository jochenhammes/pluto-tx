"""Backend-agnostic RX device abstraction.

Deliberately simpler than pluto_tx/devices/base.py's TxDevice: RX can't
radiate, so none of the PTT-sequencing/power-ceiling/safe-state machinery
that exists there for a reason applies here. A GainStage is pure signal-
chain gain, not a safety-clamped TX power level -- there's no "ceiling" or
"off_value" concept, and no prepare_for_start()/force_safe_state() hooks.
"""
import abc
from dataclasses import dataclass
from typing import Optional, Union

from ..fft_probe import FftProbe


@dataclass(frozen=True)
class GainStage:
    """One independently settable RX gain control on a device.

    controls_agc=True marks the (at most one) stage an AGC mode switch acts
    on -- e.g. Pluto's single gain stage, which set_gain_mode() switches
    between "manual" and the AD9361's hardware AGC modes. A device with no
    AGC capability at all (confirmed for HackRF's RX side this session:
    hasGainMode(0)=False) has controls_agc=False on every stage."""
    name: str
    label: str
    kind: str  # "continuous_db" | "bool"
    min_value: float
    max_value: float
    step: float  # 0.0 = continuous/auto-stepped, no fixed increment
    unit: str
    default_value: Union[float, bool]
    controls_agc: bool


class RxDevice(abc.ABC):
    """One connected RX-capable SDR. Constructed with connection parameters
    only -- build_source() performs the actual (side-effecting) hardware/GNU
    Radio setup, called exactly once by the flowgraph right before wiring
    the returned block into the demod chain."""

    device_type: str  # registry key, e.g. "pluto" | "hackrf" | "rtlsdr"
    display_name: str  # GUI label, e.g. "PlutoSDR"
    connection_kind: str  # "uri" | "soapy_args" -- which GUI connection widget to show
    frequency_range_hz: tuple
    # Audio-domain analogue of frequency_range_hz, for a backend with no RF
    # LO to retune but a real DSP frequency shift makes sense anyway (e.g.
    # AudioDevice's incoming sound-card spectrum, where a narrowband
    # PSK31/RADE/SSB signal can sit at any tone). None (the default) means
    # no baseband-tuning concept -- kept SEPARATE from frequency_range_hz
    # rather than repurposing (0.0, 0.0), since is_audio_only() below must
    # keep meaning "no RF" for its existing consumers (File Broadcast
    # gating, etc.) regardless of this.
    audio_tuning_range_hz: Optional[tuple] = None
    # Upper bound for the waterfall's zoom slider -- FftProbe.MAX_ZOOM by
    # default. A backend with a much lower sample rate than RF's needs a
    # tighter cap: zoom is a real zoom-FFT (fft_probe.py), needing
    # fft_size*zoom FRESH samples per output row, so row rate =
    # sample_rate/(fft_size*zoom) degrades far faster at a low sample rate
    # than at RF's multi-MHz ones for the same zoom number -- see
    # AudioDevice's override for the concrete math.
    max_waterfall_zoom: int = FftProbe.MAX_ZOOM
    sample_rate_hz_choices: tuple  # flat, curated list -- see PlutoDevice's docstring for why
    default_sample_rate_hz: float
    default_bandwidth_hz: Optional[float]  # None = backend has no exposed concept
    gain_stages: tuple  # tuple[GainStage, ...]
    supports_agc_mode: bool
    agc_modes: tuple  # empty unless supports_agc_mode
    default_gain_mode: Optional[str]
    # True only for backends whose driver exposes DC-offset/IQ-imbalance
    # correction (confirmed True for Pluto's AD9361; confirmed False for
    # HackRF's SoapyHackRF driver this session -- no has_dc_offset_mode/
    # has_iq_balance_mode).
    supports_dc_iq_correction: bool = False
    # True only for backends with a client-side libiio buffer-size knob
    # worth exposing (currently just Pluto -- see PlutoDevice/config.py's
    # PLUTO_RX_BUFFER_SIZE_* for the real-hardware throughput story).
    # gui.py gates the "Buffer Size" combo's visibility on this, mirroring
    # supports_agc_mode's own capability-flag idiom rather than a
    # hardcoded device_type == "pluto" check.
    supports_buffer_size: bool = False

    def __init__(self, connection: str, frequency_hz: float, sample_rate_hz: float,
                 bandwidth_hz: Optional[float], buffer_size: Optional[int] = None):
        self.connection = connection
        self.frequency_hz = frequency_hz
        self.sample_rate_hz = sample_rate_hz
        self.bandwidth_hz = bandwidth_hz
        # Harmless no-op for backends with supports_buffer_size=False --
        # only PlutoDevice.build_source() actually reads this.
        self.buffer_size = buffer_size

    @classmethod
    def is_audio_only(cls) -> bool:
        """True for backends with no RF concept at all (frequency_range_hz
        == (0.0, 0.0)) -- e.g. AudioDevice. Mirrors pluto_tx's
        TxDevice.is_audio_only() exactly. Gates whether a device can carry
        a wideband-RF-native feature like File Broadcast's GFSK branch,
        which assumes real RF bandwidth (hundreds of kHz) to design its
        resampler against -- a real bug found on real use: AudioDevice's
        20kHz sample rate made firdes.low_pass's cutoff (225kHz, derived
        from FILEBROADCAST_WORKING_RATE_HZ) exceed sample_rate/2, raising
        IndexError at construction time and making Soundcard mode
        completely unable to connect."""
        return cls.frequency_range_hz == (0.0, 0.0)

    def close(self):
        """Release resources that outlive the flowgraph (e.g. a network
        connection to a single-client server). No-op by default; called by
        AdvancedRxFlowgraph.shutdown()."""
        pass

    def connection_status(self):
        """None if this backend has no notion of a link that can drop, else a
        short string ("connected" / "connecting" / "lost") the GUI surfaces."""
        return None

    @abc.abstractmethod
    def build_source(self):
        """Construct and configure the GNU Radio source block and return it
        for the caller to wire into the demod chain. Called exactly once per
        device instance. Gain/gain-mode are NOT set here -- the flowgraph
        calls set_gain_mode()/set_gain() right afterward, mirroring
        TxDevice.build_sink()'s "construct first, configure power after"
        split."""

    @abc.abstractmethod
    def set_frequency(self, freq_hz: float):
        ...

    @abc.abstractmethod
    def set_gain(self, stage_name: str, value):
        ...

    def set_gain_mode(self, mode: str):
        """No-op unless supports_agc_mode=True -- concrete default here so
        callers don't need an extra hasattr()/isinstance() check for
        backends with no AGC (e.g. HackRF's RX side)."""
        pass

    @abc.abstractmethod
    def read_hw_state(self) -> dict:
        """Live hardware state for future GUI status readback. Keys are
        backend-specific; a device with nothing to report returns {}."""

    @staticmethod
    @abc.abstractmethod
    def probe_with_timeout(connection: str, timeout_s: float):
        """Return None if `connection` is reachable, else the Exception
        reachability failed with (bounded by timeout_s, never blocks
        longer)."""

    @staticmethod
    @abc.abstractmethod
    def scan_devices_with_timeout(timeout_s: float):
        """Return (devices, None) on success -- a dict of connection string
        -> human-readable label -- or (None, exception) on failure/timeout."""
