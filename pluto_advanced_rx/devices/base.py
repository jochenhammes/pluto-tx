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

    def __init__(self, connection: str, frequency_hz: float, sample_rate_hz: float,
                 bandwidth_hz: Optional[float]):
        self.connection = connection
        self.frequency_hz = frequency_hz
        self.sample_rate_hz = sample_rate_hz
        self.bandwidth_hz = bandwidth_hz

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
