"""Shared constants for the PlutoSDR TX app."""

DEFAULT_URI = "ip:plutoplus.local"

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
DEFAULT_NF_GAIN = 1.0  # manual audio drive multiplier, applied after the AGC

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
