"""Pure-function RF-domain analysis for RADE V1's "Auto Fine-Tune" feature
(gui.py's _autotune_* state machine). Deliberately Qt/GNU-Radio-free -- same
separation as fft_probe.py/dynamics.py -- so it's testable with synthetic
FFT rows alone.

Both functions operate on a single already-computed FftProbe row (dB
magnitude, one float per FFT bin) plus the frequency axis parameters
(center_hz, span_hz) needed to map bin index -> absolute frequency --
exactly the same mapping AdvancedWaterfallWidget.push_fft_row() already
uses for its spectrum curve, duplicated here rather than imported since
that method is bound up with the widget's own state (image buffer, curve
object) this module has no business touching.
"""
import numpy as np


def _window_slice(row_db, center_hz, span_hz, freq_hz, radius_hz):
    """Bin index range [lo, hi) covering [freq_hz-radius_hz, freq_hz+radius_hz],
    clamped to the row's actual bin range."""
    fft_size = len(row_db)
    bin_hz = span_hz / fft_size
    center_bin = fft_size // 2 + (freq_hz - center_hz) / bin_hz
    half_bins = radius_hz / bin_hz
    lo = max(0, int(round(center_bin - half_bins)))
    hi = min(fft_size, int(round(center_bin + half_bins)))
    return lo, hi


def _noise_floor_excluding(row_db, center_hz, span_hz, freq_hz, radius_hz, exclude_radius_hz, percentile=20.0):
    """This window's local noise floor (given percentile), computed from
    bins OUTSIDE [freq_hz-exclude_radius_hz, freq_hz+exclude_radius_hz] but
    still inside [freq_hz-radius_hz, freq_hz+radius_hz] -- unlike a plain
    percentile over the WHOLE window, this excludes the region where the
    signal itself is expected to sit, so the signal's own energy doesn't
    inflate the floor estimate. A real bug found and fixed this session:
    RADE's OFDM signal (~1.45kHz wide, see config.RADE_OFDM_LOW_HZ/HIGH_HZ)
    can dominate a naive +-2kHz search window's own percentile, biasing both
    the centering estimate and the gain-SNR correction. Falls back to the
    full (non-excluding) window if too few bins remain outside the excluded
    region (e.g. exclude_radius_hz >= radius_hz)."""
    lo, hi = _window_slice(row_db, center_hz, span_hz, freq_hz, radius_hz)
    if hi <= lo:
        return float(np.percentile(row_db, percentile))
    elo, ehi = _window_slice(row_db, center_hz, span_hz, freq_hz, exclude_radius_hz)
    outer = np.concatenate([row_db[lo:elo], row_db[ehi:hi]])
    if len(outer) < 3:
        return float(np.percentile(row_db[lo:hi], percentile))
    return float(np.percentile(outer, percentile))


def estimate_signal_center(row_db, center_hz, span_hz, freq_hz, radius_hz, threshold_db=6.0,
                            exclude_radius_hz=None):
    """Power-weighted centroid frequency of the bins inside
    [freq_hz-radius_hz, freq_hz+radius_hz] that sit at least threshold_db
    above the window's local noise floor -- deliberately a centroid, not
    argmax: RADE's OFDM signal has energy spread across several
    subcarriers, not one narrow spike, so a weighted center of mass is a
    better estimate of the channel's true center than "the single loudest
    bin". Returns None if fewer than 3 bins clear the threshold (too little
    signal to trust an estimate from).

    exclude_radius_hz: if given, the floor is computed via
    _noise_floor_excluding() (signal-region excluded) instead of the whole
    window's own percentile -- see that function's docstring; pass RADE's
    expected occupied half-bandwidth plus a mistuning margin here, not left
    at the old whole-window default, when calling this for RADE."""
    fft_size = len(row_db)
    bin_hz = span_hz / fft_size
    lo, hi = _window_slice(row_db, center_hz, span_hz, freq_hz, radius_hz)
    if hi - lo < 3:
        return None
    window = row_db[lo:hi]
    if exclude_radius_hz is not None:
        floor_db = _noise_floor_excluding(row_db, center_hz, span_hz, freq_hz, radius_hz, exclude_radius_hz)
    else:
        floor_db = np.percentile(window, 20)
    mask = window >= (floor_db + threshold_db)
    if np.count_nonzero(mask) < 3:
        return None
    bin_indices = np.arange(lo, hi)[mask]
    # Weight by LINEAR power, not the dB values themselves -- dB is already
    # a log quantity, weighting by it would under-emphasize the true peak.
    weights = 10.0 ** (window[mask] / 10.0)
    centroid_bin = float(np.average(bin_indices, weights=weights))
    return center_hz + (centroid_bin - fft_size // 2) * bin_hz


def measure_peak_db(row_db, center_hz, span_hz, freq_hz, radius_hz):
    """Peak (max) dB value within [freq_hz-radius_hz, freq_hz+radius_hz]."""
    lo, hi = _window_slice(row_db, center_hz, span_hz, freq_hz, radius_hz)
    if hi <= lo:
        return float(np.max(row_db))
    return float(np.max(row_db[lo:hi]))


def measure_noise_floor_db(row_db, center_hz, span_hz, freq_hz, radius_hz, exclude_radius_hz=None):
    """This window's local noise floor (20th percentile) -- same robust
    estimate estimate_signal_center() uses for its own threshold.

    exclude_radius_hz: if given, delegates to _noise_floor_excluding()
    (signal-region excluded) instead of the whole window's own percentile --
    see that function's docstring."""
    if exclude_radius_hz is not None:
        return _noise_floor_excluding(row_db, center_hz, span_hz, freq_hz, radius_hz, exclude_radius_hz)
    lo, hi = _window_slice(row_db, center_hz, span_hz, freq_hz, radius_hz)
    if hi <= lo:
        return float(np.percentile(row_db, 20))
    return float(np.percentile(row_db[lo:hi], 20))


def measure_peak_snr_db(row_db, center_hz, span_hz, freq_hz, radius_hz, exclude_radius_hz=None):
    """Peak signal level relative to this window's own local noise floor --
    NOT an absolute dB reading. Deliberately relative: FftProbe's dB scale
    is unnormalized FFT output (no /fft_size or window-gain correction), so
    its absolute magnitude shifts by tens of dB with fft_size/zoom (a real
    bug caught calibrating this feature on real hardware: a fixed absolute
    target read a genuinely overloaded RTL-SDR front-end as needing MORE
    gain, when it already needed less). peak_db and floor_db both carry
    that same offset, so their difference cancels it out -- a real SNR-like
    quantity, portable across whatever fft_size/zoom happens to be active.

    exclude_radius_hz: forwarded to measure_noise_floor_db() -- see its
    docstring and _noise_floor_excluding()'s for the signal-overlap bug this
    guards against."""
    peak = measure_peak_db(row_db, center_hz, span_hz, freq_hz, radius_hz)
    floor = measure_noise_floor_db(row_db, center_hz, span_hz, freq_hz, radius_hz, exclude_radius_hz)
    return peak - floor
