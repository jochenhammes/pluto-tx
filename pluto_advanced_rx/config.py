"""Shared constants for the PlutoSDR advanced RX app.

Deliberately a SELF-CONTAINED COPY of pluto_rx/config.py's RX-tuning values
(not an import) -- pluto_advanced_rx is a separate, independent app that
should be free to diverge from pluto_rx without risking the stable app, the
same relationship pluto_rx itself has to pluto_tx. Only genuinely generic,
non-RX-specific constants/helpers are still re-exported from pluto_tx.config.
"""
from gnuradio.fft import window

from pluto_tx.config import DEFAULT_URI, DE_AMATEUR_BANDS_HZ, in_amateur_band, normalize_uri  # noqa: F401 (re-exported)

# RX baseband ("quadrature") rate presets -- these double as the waterfall's
# "zoom levels": each is the actual AD9361 RX sample rate (and, in "Auto"
# filter mode, the analog RX filter bandwidth the driver derives from it).
# Chosen as clean multiples of DEMOD_IF_RATE so the IF decimation stage below
# always lands on an integer decimation factor. Changing this requires a full
# flowgraph rebuild (GNU Radio FIR/resampler blocks can't change their
# decimation ratio at runtime) -- see gui.py's _on_bandwidth_changed.
#
# Extended beyond pluto_rx's [1M, 2.5M] on request, up to what actually
# produces data on real hardware -- each preset here was re-verified.
# 5M/8M/10M DO work but show real buffer overruns ("O" printed by GNU Radio)
# and audio underruns: the CURRENT ip:plutoplus.local / IIOD-network-protocol
# connection has a measured throughput ceiling around ~4.7-4.9 Msps (same
# finding pluto_rx/README already documents) -- expect choppy audio/gaps in
# the waterfall at these presets until a native USB backend is used instead
# (see README ToDo).
#
# 15M/20M were tried and are NOT included: at that decimation ratio (400:1
# down to DEMOD_IF_RATE) the auto-designed IF filter grows to >13,000 taps,
# which exceeds what GNU Radio's scheduler buffer can feed it per work()
# call -- not just overruns, but a hard scheduler error and ZERO data
# produced. Increasing buffer sizes alone does not fix this (tried,
# confirmed insufficient); it needs a multi-stage/cascaded decimation
# instead of the current single-stage rational_resampler_ccf, which is real
# rework, not a config change -- see README ToDo.
RX_BANDWIDTH_PRESETS = [1_000_000, 2_500_000, 5_000_000, 8_000_000, 10_000_000]
DEFAULT_RX_BANDWIDTH = 2_500_000

# Fixed IF rate the demodulator chain always runs at, regardless of which
# RX_BANDWIDTH_PRESETS entry is selected -- keeps the FM/SSB demod + audio
# resampler chain identical across zoom levels; only the IF decimation
# stage's ratio changes per bandwidth.
DEMOD_IF_RATE = 50_000

AUDIO_RATE = 48_000
FM_DEVIATION_HZ = 2500.0  # matches pluto_tx's narrowband voice FM default
SSB_AUDIO_BAND_HZ = (300.0, 2700.0, 300.0)  # (f_lo, f_hi, trans_width), USB

# FM audio low-pass filter (demod noise cleanup above the voice band, AFTER
# quadrature_demod_cf -- distinct from FM_DEMOD_WIDTH below, which band-limits
# the IF/RF signal BEFORE demod).
FM_AUDIO_CUTOFF_HZ = 3000.0
FM_AUDIO_TRANS_HZ = 500.0

# --- Demodulator width: an actual, operator-adjustable channel filter, not
# just a display estimate. FM: a real/low-pass-shaped filter (fir_filter_ccc
# with real low-pass taps, symmetric around 0 Hz -- a standard technique for
# band-limiting a complex signal) applied to the IF signal BEFORE
# quadrature_demod_cf, i.e. the actual RF/IF channel width. SSB: directly
# the width of the existing complex_band_pass demodulator filter (f_hi - f_lo,
# with f_lo held fixed -- widening/narrowing extends f_hi). Both are
# runtime-adjustable via set_taps() on the already-connected filter blocks,
# no flowgraph rebuild needed. The waterfall's demod-band overlay is driven
# by these same values, so it now shows the ACTUAL filter width, not an
# estimate.
FM_DEMOD_WIDTH_DEFAULT_HZ = 12_500.0  # standard NBFM channel spacing
FM_DEMOD_WIDTH_RANGE_HZ = (2_500.0, 20_000.0)
FM_CHANNEL_TRANS_HZ = 1_000.0

SSB_DEMOD_WIDTH_DEFAULT_HZ = 3_000.0
SSB_DEMOD_WIDTH_RANGE_HZ = (1_000.0, 5_000.0)

DEFAULT_FREQUENCY = 432_150_000  # Hz, matches the TX app's default test frequency
FINE_TUNE_RANGE_HZ = 2_000  # +/- range of the fine-tune slider, same as the TX app

GAIN_MODES = ["manual", "slow_attack", "fast_attack", "hybrid"]
DEFAULT_GAIN_MODE = "slow_attack"
DEFAULT_MANUAL_GAIN_DB = 40.0
MANUAL_GAIN_RANGE_DB = (0.0, 73.0)  # AD9361 RX1 gain table range

DEFAULT_NF_GAIN = 1.0  # audio volume multiplier, applied after the demodulator

FFT_SIZE_PRESETS = [1024, 2048, 4096, 8192, 16384]
DEFAULT_FFT_SIZE = 1024

# --- Waterfall widget (pyqtgraph) -------------------------------------------
WATERFALL_HISTORY_ROWS = 200  # rolling time-history depth of the waterfall image
WATERFALL_POLL_INTERVAL_MS = 33  # ~30 Hz GUI-side poll of fft_probe's latest row
FFT_COMPUTE_RATE_HZ = 30  # fft_probe's own compute throttle, independent of poll rate/sample rate
WATERFALL_WINDOW = window.WIN_BLACKMAN_hARRIS  # matches pluto_rx's qtgui.waterfall_sink_c window
WATERFALL_COLORMAP = "viridis"
WATERFALL_DB_RANGE = (-80.0, 0.0)  # fixed color/Y-axis levels (no per-frame autoscale)
