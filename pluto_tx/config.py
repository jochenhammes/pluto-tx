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

# --- M17 digital voice (optional -- needs gr-m17, see install-m17.sh) ------
# Parameters below are taken from gr-m17's own real reference flowgraphs
# (examples/transmitterPLUTOSDR.grc), not re-derived from the M17 spec, and
# verified this session by an actual offline m17_coder->m17_decoder
# round-trip (encoded test bytes decoded correctly, including src/dst
# callsigns recovered from the LSF). The M17 branch deliberately bypasses
# nf_filter/gate/agc/compressor/nf_gain/limiter_smooth/limiter (the analog-
# modulation dynamics chain) -- it taps ptt_mute's output directly. Codec2
# has its own internal level handling; a broadcast-style compressor ahead of
# a low-bitrate vocoder is more likely to hurt intelligibility than help.
M17_CODEC2_RATE = 8_000  # codec2's fixed input rate
M17_SYMBOL_RATE = 4_800  # M17's native baud rate
M17_RRC_ALPHA = 0.5
M17_RRC_NTAPS = 81
M17_RRC_SPS = 10  # samples/symbol after RRC pulse shaping
M17_BASEBAND_RATE = M17_SYMBOL_RATE * M17_RRC_SPS  # 48,000 Hz, post-RRC/pre-FM-mod
M17_DEVIATION_HZ = 800.0  # FM deviation for the outer (+-1) symbol level

M17_DEFAULT_DST_CALLSIGN = "@ALL"  # standard M17 broadcast destination
M17_CALLSIGN_MAX_LEN = 9  # m17_coder truncates to this; GUI should validate the same

# unkey_ptt() in M17 mode can't cut RF instantly like FM/SSB -- the encoder
# needs a short tail (>= 1 final frame + eot_cnt EOT frames, ~80ms minimum
# with defaults) to actually transmit a clean EOT so the receiver doesn't
# hang. M17_EOT_HOLD_S is how long the GUI waits before lowering
# attenuation; M17_EOT_HOLD_WATCHDOG_S is a hard ceiling in case something
# goes wrong. Both are conservative placeholders -- gr-m17's real-hardware
# tail latency (scheduling/USB/IIOD buffering on top of the ~80ms
# theoretical minimum) is unmeasured; needs calibration against a real PTT
# release, which requires explicit operator approval to test (see README).
M17_EOT_HOLD_S = 0.4
M17_EOT_HOLD_WATCHDOG_S = 2.0

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
