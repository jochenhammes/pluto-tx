"""Pure-function FT8 audio encoder: renders a plain-text FT8 message (e.g.
"CQ DA2JH JO31") into a real audio waveform (8-tone Gaussian-filtered
continuous-phase FSK, GFSK, at the fixed 31.25/... no -- FT8's fixed
6.25 baud symbol rate) suitable for injection into this app's existing
Hilbert-based USB modulation chain -- same architectural role as
digitext.py's/psk31.py's own encode_text()/encode_message(), same
(audio, duration_s) return shape.

Message packing (text -> 77-bit payload -> 79 tone indices 0..7) is done
by ft8_ctypes.encode_message(), which calls into kgoba/ft8_lib (a real,
existing open-source FT8 codec, built by install-ft8.sh) -- NOT
reimplemented here. This module's own job is exactly one thing: turn
those 79 tone indices into an actual audio waveform, ourselves, in pure
Python/numpy.

## GFSK synthesis

Ported directly from ft8_lib's own reference implementation
(demo/gen_ft8.c: gfsk_pulse()/synth_gfsk()), not derived independently --
this project's own established discipline for protocol-level DSP (see
psk31.py's own Varicode-table provenance note). The core idea: each
symbol's tone contributes a raised/Gaussian-smoothed FREQUENCY deviation
(not a phase jump) spread across a 3-symbol-wide window centered on that
symbol, so neighboring symbols' contributions overlap and blend --
integrating this smoothed instantaneous frequency over time (cumulative
phase) is what gives GFSK its continuous phase and tight spectral
occupancy (the whole reason FT8 fits 8 tones in just 50Hz). BT=2.0 (the
Gaussian filter's bandwidth-time product) is FT8's own fixed value
(FT4 uses BT=1.0, not used here).

Exact numerical fidelity to the reference matters here (unlike e.g.
psk31.py's own from-scratch raised-cosine choice, which had no single
"correct" reference to match) -- this signal must be decodable by ANY
real FT8 receiver (this project's own RX included, but also real WSJT-X
stations), so this is a case for porting the actual published algorithm,
not inventing an equivalent-sounding one."""
import math

import numpy as np

from . import ft8_ctypes

FT8_SYMBOL_PERIOD = ft8_ctypes.FT8_SYMBOL_PERIOD  # 0.16s, fixed by FT8 protocol
FT8_SLOT_TIME = ft8_ctypes.FT8_SLOT_TIME          # 15.0s, fixed by FT8 protocol
FT8_TONE_SPACING_HZ = ft8_ctypes.FT8_TONE_SPACING_HZ  # 6.25Hz, fixed by FT8 protocol
FT8_NN = ft8_ctypes.FT8_NN                        # 79 channel symbols, fixed by FT8 protocol

# Gaussian pulse-shaping bandwidth-time product -- FT8's own fixed value
# (ft8_lib's demo/gen_ft8.c: `#define FT8_SYMBOL_BT 2.0f`; FT4 uses 1.0,
# not applicable here).
FT8_SYMBOL_BT = 2.0
# == pi * sqrt(2 / log(2)) -- ft8_lib's own GFSK_CONST_K (demo/gen_ft8.c),
# the normalization constant for a Gaussian pulse expressed via erf().
_GFSK_CONST_K = 5.336446


def _gfsk_pulse(n_spsym: int, symbol_bt: float) -> np.ndarray:
    """Direct port of ft8_lib's gfsk_pulse() (demo/gen_ft8.c): the
    Gaussian smoothing pulse a single symbol's tone contributes to the
    instantaneous-frequency waveform, truncated at 3 symbol lengths (the
    pulse is theoretically infinite, real spectral energy beyond 3
    symbols is negligible). math.erf() (not numpy, which has no built-in
    erf) is fine here -- this pulse array is only 3*n_spsym samples
    (a few thousand at most), computed once per TX render, not per
    sample of the final (much longer) audio waveform."""
    n = 3 * n_spsym
    t = np.arange(n, dtype=np.float64) / n_spsym - 1.5
    arg1 = _GFSK_CONST_K * symbol_bt * (t + 0.5)
    arg2 = _GFSK_CONST_K * symbol_bt * (t - 0.5)
    erf = np.vectorize(math.erf)
    return (erf(arg1) - erf(arg2)) / 2


def _synth_gfsk(symbols: np.ndarray, f0_hz: float, symbol_bt: float,
                 symbol_period: float, sample_rate: float) -> np.ndarray:
    """Direct port of ft8_lib's synth_gfsk() (demo/gen_ft8.c). symbols:
    tone indices (0..7 for FT8). Returns real audio samples (unnormalized
    sin() output, amplitude ~1.0, matching the reference exactly --
    encode_message() below applies the operator-facing amplitude scale)."""
    n_sym = len(symbols)
    n_spsym = int(round(sample_rate * symbol_period))
    n_wave = n_sym * n_spsym
    dphi_peak = 2 * np.pi / n_spsym  # hmod = 1.0 (one tone-spacing unit of deviation per symbol value)

    # Base carrier phase increment everywhere, plus each symbol's own
    # smoothed contribution added on top (overlapping 3-symbol windows).
    dphi = np.full(n_wave + 2 * n_spsym, 2 * np.pi * f0_hz / sample_rate, dtype=np.float64)
    pulse = _gfsk_pulse(n_spsym, symbol_bt)
    for i in range(n_sym):
        ib = i * n_spsym
        dphi[ib:ib + 3 * n_spsym] += dphi_peak * symbols[i] * pulse

    # Dummy symbols at beginning/end (reference's own edge-extension trick,
    # using the first/last real symbol's own tone for the phantom
    # neighbor) so the smoothing window has something to blend with right
    # at the very start/end of the signal.
    dphi[:2 * n_spsym] += dphi_peak * pulse[n_spsym:3 * n_spsym] * symbols[0]
    end = n_sym * n_spsym
    dphi[end:end + 2 * n_spsym] += dphi_peak * pulse[:2 * n_spsym] * symbols[-1]

    # Cumulative phase integration -- signal[k] uses the phase accumulated
    # BEFORE this sample's own increment (matches the reference's
    # `signal[k] = sin(phi); phi += dphi[k+n_spsym]` ordering exactly);
    # fmodf(..., 2*pi) in the original is a numerical-range nicety with no
    # effect on sin()'s own output, safely dropped in this vectorized port.
    phase_increments = dphi[n_spsym:n_spsym + n_wave]
    phi_at_k = np.cumsum(phase_increments) - phase_increments
    signal = np.sin(phi_at_k)

    # Raised-cosine (half-Hann) ramp at the very start/end, same as the
    # reference -- a clean key-up/key-down envelope.
    n_ramp = n_spsym // 8
    if n_ramp > 0:
        i = np.arange(n_ramp)
        env = (1 - np.cos(2 * np.pi * i / (2 * n_ramp))) / 2
        signal[:n_ramp] *= env
        signal[n_wave - n_ramp:n_wave] *= env[::-1]

    return signal.astype(np.float32)


def encode_message(text: str, sample_rate: float, tone_hz: float, amplitude: float = 0.7,
                    pad_to_slot: bool = True):
    """Returns (audio: np.ndarray[float32], duration_s: float), same shape
    as digitext.encode_text()/psk31.encode_text(). Returns (None, 0.0) if
    the message can't be packed (see ft8_ctypes.encode_message()'s own
    contract -- e.g. a malformed callsign/grid).

    pad_to_slot: append trailing silence so the returned audio always
    fills a full FT8_SLOT_TIME (15s) -- the real signal itself is only
    FT8_NN*FT8_SYMBOL_PERIOD = 12.64s, deliberately shorter than the 15s
    slot (the remaining ~2.36s is real, required silence: time for any
    receiver -- including a real WSJT-X station -- to run its own decode
    before the next slot starts). Set False for a caller that wants to
    handle slot-filling/timing itself (e.g. a software round-trip test
    that doesn't care about wall-clock slot alignment at all)."""
    tones = ft8_ctypes.encode_message(text)
    if tones is None:
        return None, 0.0
    audio = _synth_gfsk(tones, tone_hz, FT8_SYMBOL_BT, FT8_SYMBOL_PERIOD, sample_rate)
    audio *= amplitude
    if pad_to_slot:
        signal_s = FT8_NN * FT8_SYMBOL_PERIOD
        pad_samples = int(round((FT8_SLOT_TIME - signal_s) * sample_rate))
        if pad_samples > 0:
            audio = np.concatenate([audio, np.zeros(pad_samples, dtype=np.float32)])
        duration_s = FT8_SLOT_TIME
    else:
        duration_s = len(audio) / sample_rate
    return audio, duration_s
