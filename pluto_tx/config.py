"""Shared constants for the PlutoSDR TX app."""

DEFAULT_URI = "ip:plutoplus.local"

_URI_SCHEMES = ("ip:", "usb:", "local:", "xml:")


def normalize_uri(text: str) -> str:
    """Accept a bare hostname/IP (e.g. 'plutoplus.local', '192.168.1.50') and
    turn it into a libiio network context URI ('ip:...'). An already explicit
    scheme (ip:/usb:/local:/xml:) is left untouched, so typing 'usb:1.5.5'
    still targets a specific USB device directly -- useful when more than one
    Pluto is reachable (e.g. two on the same LAN, or one on USB + one on
    Ethernet)."""
    text = text.strip()
    if not text or text.startswith(_URI_SCHEMES):
        return text
    return f"ip:{text}"

# AD9361 TX attenuation range (dB, 0 = max power, more negative = less power).
MIN_ATTEN = -89.75
MAX_ATTEN = 0.0
# GUI power slider default ceiling: never select less attenuation than this
# (i.e. never more power than this) unless the "unlock full power" box is checked.
DEFAULT_ATTEN_CEILING = -20.0

# Audio front end.
AUDIO_RATE = 48_000
# Shared TX baseband ("quadrature") rate for both FM and SSB. fmcomms2_sink
# needs set_filter_params() to go below the AD9361's hardware ADC/DAC floor
# (~2.083 MHz) -- we don't configure that, so QUAD_RATE must stay >= that
# floor. 2.5 MSps also matches the rate already verified working earlier
# this session (RX capture + the SDRangel TX test).
QUAD_RATE = 2_500_000

DEFAULT_BANDWIDTH = 200_000  # Hz, AD9361 analog TX filter (min allowed is 200000)
DEFAULT_FREQUENCY = 432_150_000  # Hz, matches today's verified carrier test
FINE_TUNE_RANGE_HZ = 2_000  # +/- range of the fine-tune spinbox

# NF (audio) band-pass filter presets, (f_lo, f_hi, trans_width) in Hz.
NF_FILTER_PRESETS = {
    "FM": (300.0, 3000.0, 300.0),
    "SSB": (300.0, 2700.0, 300.0),
}

FM_DEVIATION_HZ = 2500.0  # narrowband voice FM default
DEFAULT_NF_GAIN = 1.0  # manual audio drive multiplier, applied after the compressor

# --- NF dynamics processing: noise gate, compressor, smooth limiter -------
# Signal order: ptt_mute -> nf_filter -> gate -> agc -> compressor ->
# nf_gain -> limiter_smooth -> limiter (existing hard-clip safety backstop,
# unchanged). See pluto_tx/dynamics.py for the algorithm and pluto_tx/
# flowgraph.py for the wiring. Values below are starting points from general
# broadcast-audio convention, not yet re-validated against this project's
# actual mic/WAV levels -- treat as a reasonable default, not gospel.
GATE_THRESHOLD_DB = -50.0
GATE_ALPHA = 0.0001  # analog.pwr_squelch_ff's internal averaging filter gain
GATE_RAMP_SAMPLES = 480  # ~10ms at AUDIO_RATE, sinusoidal attack/release ramp
GATE_BYPASS_THRESHOLD_DB = -100.0  # pwr_squelch_ff has no enable/disable API; this
# floor threshold is the bypass trick -- effectively never gates (see set_gate_enabled)

COMPRESSOR_THRESHOLD_DB = -18.0
COMPRESSOR_RATIO = 4.0  # whole number: the GUI's ratio slider is integer-stepped
COMPRESSOR_KNEE_DB = 6.0
COMPRESSOR_ATTACK_MS = 8.0
COMPRESSOR_RELEASE_MS = 120.0

LIMITER_THRESHOLD_DB = -3.0
LIMITER_RATIO = 20.0
LIMITER_KNEE_DB = 1.0
LIMITER_ATTACK_MS = 1.0
LIMITER_RELEASE_MS = 60.0

# German amateur radio band edges reachable by the Pluto's TX LO range
# (46.875 MHz - 6 GHz), used only for a non-blocking sanity warning in the GUI.
DE_AMATEUR_BANDS_HZ = [
    ("2m", 144_000_000, 146_000_000),
    ("70cm", 430_000_000, 440_000_000),
    ("23cm", 1_240_000_000, 1_300_000_000),
]


def in_amateur_band(freq_hz: float):
    """Return the band name containing freq_hz, or None if out of all known bands."""
    for name, lo, hi in DE_AMATEUR_BANDS_HZ:
        if lo <= freq_hz <= hi:
            return name
    return None
