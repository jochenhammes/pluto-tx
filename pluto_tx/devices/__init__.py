"""TX device backend registry. Adding a new backend: implement TxDevice in a
new module here, register it below -- no changes needed to flowgraph.py."""
from .base import PowerStage, TxDevice
from .pluto import PlutoDevice
from .hackrf import HackRFDevice
from .soundcard import SoundcardDevice

DEVICE_REGISTRY = {
    "pluto": PlutoDevice,
    "hackrf": HackRFDevice,
    "soundcard": SoundcardDevice,
}

# AIOC needs pyserial (python3-serial), not a hard dependency of this app's other
# backends -- same defensive posture as flowgraph.py's LORA_AVAILABLE/M17_AVAILABLE
# etc: degrade to "device just doesn't appear" rather than break Pluto/HackRF/
# Soundcard on a system that hasn't installed it yet (see install.sh).
try:
    from .aioc import AiocDevice
    DEVICE_REGISTRY["aioc"] = AiocDevice
    AIOC_AVAILABLE = True
except ImportError:
    AIOC_AVAILABLE = False


def primary_power_stage(device_type):
    """The PowerStage with is_primary=True for a device type, without
    needing an actual device instance -- power_stages is a class attribute."""
    return next(s for s in DEVICE_REGISTRY[device_type].power_stages if s.is_primary)


def build_device(device_type, connection=None, frequency_hz=None, sample_rate_hz=None, bandwidth_hz=None):
    device_cls = DEVICE_REGISTRY[device_type]
    if connection is None:
        connection = device_cls.DEFAULT_CONNECTION
    if sample_rate_hz is None:
        sample_rate_hz = device_cls.default_sample_rate_hz
    if bandwidth_hz is None:
        bandwidth_hz = device_cls.default_bandwidth_hz
    return device_cls(connection, frequency_hz, sample_rate_hz, bandwidth_hz)
