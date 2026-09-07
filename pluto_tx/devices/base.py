"""Backend-agnostic TX device abstraction.

Every RF-safety-critical PTT/power sequencing DECISION (attenuation-before-
LO-bit ordering, M17's deferred EOT tail, SOT/EOT messages, ...) stays in
flowgraph.py (PlutoTxFlowgraph) -- a TxDevice subclass only exposes narrow,
backend-specific primitives (build the sink block, read/write one power
stage, device-specific pre/post-PTT hooks, the safe-state backstop). That
split keeps the safety-critical *ordering* logic in one place, inspectable
regardless of which backend is active, while each backend only has to get
its own hardware calls right.
"""
import abc
from dataclasses import dataclass
from typing import Optional, Union


@dataclass(frozen=True)
class PowerStage:
    """One independently settable TX power/gain control on a device.

    Every device has exactly one stage with is_primary=True -- the one the
    GUI's main power slider, the power ceiling ("unlock full power"), and
    PlutoTxFlowgraph.set_target_power() apply to. A device may have further,
    non-primary stages (e.g. HackRF's coarse AMP on/off) that the GUI shows
    as separate controls but that set_target_power()/the power ceiling never
    touch directly.

    Convention, confirmed to hold for every backend implemented so far:
    max_value means MORE RF power, min_value/off_value means less/none --
    i.e. a single generic clamp (max(min_value, min(ceiling, value))) works
    for every device without a sign-flip special case."""
    name: str
    label: str
    kind: str  # "continuous_db" | "bool"
    min_value: float
    max_value: float
    unit: str
    is_primary: bool
    off_value: Union[float, bool]


class TxDevice(abc.ABC):
    """One connected TX-capable SDR. Constructed with connection parameters
    only -- build_sink() performs the actual (side-effecting) hardware/GNU
    Radio setup, called exactly once by the flowgraph right before wiring
    the returned block to tx_gain."""

    device_type: str  # registry key, e.g. "pluto" | "hackrf"
    display_name: str  # GUI label, e.g. "PlutoSDR" | "HackRF One"
    connection_kind: str  # "uri" | "serial" -- which GUI connection widget to show
    frequency_range_hz: tuple
    sample_rate_range_hz: tuple
    default_sample_rate_hz: float
    default_bandwidth_hz: Optional[float]  # None = backend has no exposed concept
    power_stages: tuple  # tuple[PowerStage, ...], exactly one with is_primary=True
    default_power_ceiling: float
    # True (Pluto): zeroing the primary power stage + post_unkey() genuinely
    # silences the output; the sink object can stay open for the whole
    # session. False: confirmed on real hardware that zeroing every power
    # stage this device exposes (HackRF: VGA=0, AMP=off) does NOT actually
    # stop transmission -- only tearing down and reopening the underlying
    # SoapySDR stream does (this system's installed gnuradio.soapy bindings
    # bind no activate()/deactivate() call to do that without a full
    # teardown, see HackRFDevice's module docstring). When False,
    # flowgraph.py rebuilds the device's sink block (via build_sink() again,
    # through the same lock()/disconnect()/connect()/unlock() pattern used
    # for mode switching) on every unkey/E-STOP, not just on Connect/
    # Disconnect.
    supports_persistent_sink: bool = True
    # True only for backends whose real hardware oscillator can be off by an
    # operator-noticeable amount with no automatic correction available
    # (confirmed for HackRF: no frequency-correction capability exposed by
    # this system's SoapyHackRF driver -- has_frequency_correction() is
    # False -- yet a real ~25kHz offset was observed on real hardware).
    # False (Pluto): no such issue reported, no correction control shown.
    supports_frequency_correction: bool = False

    def __init__(self, connection: str, frequency_hz: float, sample_rate_hz: float,
                 bandwidth_hz: Optional[float]):
        self.connection = connection
        self.frequency_hz = frequency_hz
        self.sample_rate_hz = sample_rate_hz
        self.bandwidth_hz = bandwidth_hz

    @property
    def primary_stage(self) -> PowerStage:
        return next(s for s in self.power_stages if s.is_primary)

    def set_frequency_correction(self, hz: float):
        """No-op unless supports_frequency_correction=True (see HackRFDevice
        for the one backend that currently overrides this) -- concrete
        default here so callers don't need an extra hasattr()/isinstance()
        check for backends that don't need it."""
        pass

    @abc.abstractmethod
    def prepare_for_start(self):
        """Called once, before build_sink() -- put the device into a state
        safe for the GNU Radio sink to be constructed in (e.g. Pluto: force
        minimum attenuation, power the LO up so the sink's own constructor,
        which enables TX channels immediately, doesn't radiate uncontrolled)."""

    @abc.abstractmethod
    def build_sink(self):
        """Construct and configure the GNU Radio sink block, at minimum
        power, and return it for the caller to wire to tx_gain. Called
        exactly once per device instance."""

    @abc.abstractmethod
    def set_frequency(self, freq_hz: float):
        ...

    @abc.abstractmethod
    def set_power(self, stage_name: str, value):
        ...

    @abc.abstractmethod
    def pre_key(self):
        """Called at the very start of key_ptt(), before audio is unmuted or
        power is raised. May block briefly (e.g. a synthesizer relock delay)."""

    @abc.abstractmethod
    def post_unkey(self):
        """Called after the flowgraph has already zeroed the primary power
        stage and muted audio -- the backend-specific EXTRA step beyond that
        (e.g. Pluto: power the LO down; see each backend for what, if
        anything, it can actually do here)."""

    @abc.abstractmethod
    def force_safe_state(self):
        """Idempotent, must never raise -- the last-resort 'go dark' call
        used by E-STOP and shutdown_safe()."""

    @abc.abstractmethod
    def read_hw_state(self) -> dict:
        """Live hardware state for the GUI's periodic status readback. Keys
        are backend-specific; the GUI renders whatever is present."""

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
