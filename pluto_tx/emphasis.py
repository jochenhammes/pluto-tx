"""First-order FM pre-/de-emphasis filters (6 dB/octave), as IIR taps for
gnuradio's filter.iir_filter_ffd(b, a, False) (standard sign convention).

Every FM voice radio and repeater de-emphasises the demodulated audio (and
pre-emphasises its own TX audio); a transmitter without pre-emphasis arrives
at a real rig with its highs ~20 dB too quiet between 300 and 3000 Hz. The
narrowband voice time constant is 750 us (corner ~212 Hz), i.e. a 6 dB/octave
slope across the whole speech band. Both filters are normalized to 0 dB at
1 kHz so switching them on/off keeps the mid-band level (and the deviation it
maps to) unchanged. Pure Python -- no scipy at runtime.
"""
import cmath
import math

NORM_HZ = 1000.0


def _bilinear_first_order(num, den, fs):
    """H(s) = (num[0]*s + num[1]) / (den[0]*s + den[1]) -> (b, a) via the bilinear transform."""
    k = 2.0 * fs
    b = [num[1] + num[0] * k, num[1] - num[0] * k]
    a = [den[1] + den[0] * k, den[1] - den[0] * k]
    return [v / a[0] for v in b], [v / a[0] for v in a]


def _normalize(b, a, fs, norm_hz):
    z1 = cmath.exp(-2j * math.pi * norm_hz / fs)
    gain = abs((b[0] + b[1] * z1) / (a[0] + a[1] * z1))
    return [v / gain for v in b], a


def response_db(b, a, fs, f_hz):
    z1 = cmath.exp(-2j * math.pi * f_hz / fs)
    return 20.0 * math.log10(abs((b[0] + b[1] * z1) / (a[0] + a[1] * z1)))


def preemphasis_taps(fs, tau_s, f_hi_hz):
    """Rising 6 dB/octave above 1/(2*pi*tau), flattening again at f_hi_hz (keeps the
    boost finite; a splatter low-pass follows anyway)."""
    b, a = _bilinear_first_order((tau_s, 1.0), (1.0 / (2.0 * math.pi * f_hi_hz), 1.0), fs)
    return _normalize(b, a, fs, NORM_HZ)


def deemphasis_taps(fs, tau_s):
    """Falling 6 dB/octave above 1/(2*pi*tau) -- what a real FM receiver does."""
    b, a = _bilinear_first_order((0.0, 1.0), (tau_s, 1.0), fs)
    return _normalize(b, a, fs, NORM_HZ)
