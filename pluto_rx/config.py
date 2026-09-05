"""Shared constants for the PlutoSDR RX app."""
from pluto_tx.config import DEFAULT_URI, DE_AMATEUR_BANDS_HZ, in_amateur_band, normalize_uri  # noqa: F401 (re-exported)

# RX baseband ("quadrature") rate presets -- these double as the waterfall's
# "zoom levels": each is the actual AD9361 RX sample rate (and, in "Auto"
# filter mode, the analog RX filter bandwidth the driver derives from it).
# Chosen as clean multiples of DEMOD_IF_RATE so the IF decimation stage below
# always lands on an integer decimation factor. Changing this requires a full
# flowgraph rebuild (GNU Radio FIR/resampler blocks can't change their
# decimation ratio at runtime) -- see gui.py's _on_bandwidth_changed.
#
# 200_000 and 500_000 are deliberately NOT in this list: both make the
# AD9361 driver raise "Unable to set BB rate" on real hardware (empirically
# tested) -- apparently no achievable decimation-filter configuration exists
# for those exact rates in "Auto" filter mode.
#
# 5_000_000 and 10_000_000 are ALSO deliberately excluded: measured with
# pluto_source -> null_sink + probe_rate (no demod/audio in the way), the
# CURRENT connection (`ip:plutoplus.local`, i.e. the IIOD network protocol
# over the USB gadget interface, not the native USB backend) tops out around
# ~4.7-4.9 Msps regardless of the requested rate, with GNU Radio reporting
# buffer overruns ("O") at both 5M and 10M -- the dropped samples are exactly
# what caused the reported choppy/pitch-warbling audio at higher bandwidths.
# 1_000_000 and 2_500_000 both measured clean (>=96% of requested, no
# overruns). Revisit this if/when the app gains native-USB ("usb:...")
# support (see README's network/USB ToDo) -- that transport may sustain more.
RX_BANDWIDTH_PRESETS = [1_000_000, 2_500_000]
DEFAULT_RX_BANDWIDTH = 2_500_000

# Fixed IF rate the demodulator chain always runs at, regardless of which
# RX_BANDWIDTH_PRESETS entry is selected -- keeps the FM/SSB demod + audio
# resampler chain identical across zoom levels; only the IF decimation
# stage's ratio changes per bandwidth.
DEMOD_IF_RATE = 50_000

AUDIO_RATE = 48_000
FM_DEVIATION_HZ = 2500.0  # matches pluto_tx's narrowband voice FM default
SSB_AUDIO_BAND_HZ = (300.0, 2700.0, 300.0)  # (f_lo, f_hi, trans_width), USB

DEFAULT_FREQUENCY = 432_150_000  # Hz, matches the TX app's default test frequency
FINE_TUNE_RANGE_HZ = 2_000  # +/- range of the fine-tune slider, same as the TX app

GAIN_MODES = ["manual", "slow_attack", "fast_attack", "hybrid"]
DEFAULT_GAIN_MODE = "slow_attack"
DEFAULT_MANUAL_GAIN_DB = 40.0
MANUAL_GAIN_RANGE_DB = (0.0, 73.0)  # AD9361 RX1 gain table range

DEFAULT_NF_GAIN = 1.0  # audio volume multiplier, applied after the demodulator

FFT_SIZE_PRESETS = [1024, 2048, 4096, 8192, 16384]
DEFAULT_FFT_SIZE = 1024
