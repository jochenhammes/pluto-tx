"""FT8 transmit side: renders a plain-text FT8 message (e.g. "CQ DA2JH JO31") into the 8-tone
Gaussian-filtered continuous-phase FSK (GFSK) waveform -- as complex baseband for the SDR path
(encode_iq(), an ideal USB signal by construction) or as real audio (encode_message()) -- plus the
standard QSO message texts (compose()) and UTC slot planning (plan_transmission()).

Message packing (text -> 77-bit payload -> 79 tone indices 0..7) is done
by ft8_ctypes.encode_message(), which calls into kgoba/ft8_lib (a real,
existing open-source FT8 codec, built by install-ft8.sh) -- NOT
reimplemented here. This module's own job is turning those 79 tone
indices into the actual waveform, in pure Python/numpy. Verified with
WSJT-X's jt9 as an independent decoder, in software and over the air.

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


def _gfsk_phase(symbols: np.ndarray, f0_hz: float, symbol_bt: float,
                symbol_period: float, sample_rate: float):
    """Direct port of ft8_lib's synth_gfsk() (demo/gen_ft8.c), split into its two halves: returns
    (phi, env) -- the continuous GFSK phase for tone indices `symbols` (0..7) on a base tone of f0_hz,
    and the raised-cosine key-up/key-down envelope. _synth_gfsk() turns them into real audio,
    encode_iq() into complex baseband."""
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
    phi = np.cumsum(phase_increments) - phase_increments

    # Raised-cosine (half-Hann) ramp at the very start/end, same as the
    # reference -- a clean key-up/key-down envelope.
    env = np.ones(n_wave)
    n_ramp = n_spsym // 8
    if n_ramp > 0:
        i = np.arange(n_ramp)
        ramp = (1 - np.cos(2 * np.pi * i / (2 * n_ramp))) / 2
        env[:n_ramp] = ramp
        env[n_wave - n_ramp:] = ramp[::-1]
    return phi, env


def _cpfsk_phase(symbols: np.ndarray, f0_hz: float, symbol_period: float, sample_rate: float):
    """Plain continuous-phase FSK (rectangular frequency pulses, no Gaussian smoothing) with the same
    conventions as _gfsk_phase(): tone spacing = one symbol rate, phase before the sample's own
    increment, the same raised-cosine key-up/key-down ramp. This is what JS8Call's Modulator sends
    (docs/js8/SPEC.md 1.2); the ramp is ours (only 1/8 symbol, against key clicks)."""
    n_spsym = int(round(sample_rate * symbol_period))
    n_wave = len(symbols) * n_spsym
    dphi = 2 * np.pi * f0_hz / sample_rate + (2 * np.pi / n_spsym) * np.repeat(
        np.asarray(symbols, dtype=np.float64), n_spsym)
    phi = np.cumsum(dphi) - dphi
    env = np.ones(n_wave)
    n_ramp = n_spsym // 8
    if n_ramp > 0:
        i = np.arange(n_ramp)
        ramp = (1 - np.cos(2 * np.pi * i / (2 * n_ramp))) / 2
        env[:n_ramp] = ramp
        env[n_wave - n_ramp:] = ramp[::-1]
    return phi, env


def gfsk_iq(symbols, tone_hz: float, symbol_period: float, bt, sample_rate: float,
            amplitude: float = 0.7) -> np.ndarray:
    """Complex baseband exp(j*phi) for 8-FSK tone indices `symbols` (tone 0 at +tone_hz, spacing
    1/symbol_period), i.e. the synthesis behind encode_iq() for other modes (JS8). bt: Gaussian
    bandwidth-time product (FT8: FT8_SYMBOL_BT), or None for plain CPFSK. Signal only, no tail."""
    symbols = np.asarray(symbols)
    if bt is None:
        phi, env = _cpfsk_phase(symbols, tone_hz, symbol_period, sample_rate)
    else:
        phi, env = _gfsk_phase(symbols, tone_hz, bt, symbol_period, sample_rate)
    return (amplitude * env * np.exp(1j * phi)).astype(np.complex64)


def _synth_gfsk(symbols: np.ndarray, f0_hz: float, symbol_bt: float,
                 symbol_period: float, sample_rate: float) -> np.ndarray:
    """Real audio (sin(phi)), exactly ft8_lib's synth_gfsk() output."""
    phi, env = _gfsk_phase(symbols, f0_hz, symbol_bt, symbol_period, sample_rate)
    return (np.sin(phi) * env).astype(np.float32)


def encode_iq(text: str, sample_rate: float, tone_hz: float, amplitude: float = 0.7, tail_s: float = 0.0):
    """Complex baseband exp(j*phi) of the message, tone 0 at +tone_hz: an ideal single-sideband signal
    by construction (no Hilbert transform involved), for the SDR TX path. Its real part is valid FT8
    audio for Soundcard/AIOC. Returns (iq: complex64, duration_s), or (None, 0.0) if the message can't
    be packed. Only the 12.64 s signal plus tail_s of silence; slot timing is the caller's job."""
    tones = ft8_ctypes.encode_message(text)
    if tones is None:
        return None, 0.0
    phi, env = _gfsk_phase(tones, tone_hz, FT8_SYMBOL_BT, FT8_SYMBOL_PERIOD, sample_rate)
    iq = amplitude * env * np.exp(1j * phi)
    n_tail = int(round(tail_s * sample_rate))
    if n_tail > 0:
        iq = np.concatenate([iq, np.zeros(n_tail)])
    return iq.astype(np.complex64), len(iq) / sample_rate


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


# --- Standard QSO messages (manual sequencing: the operator picks one per slot) ---------------------
MESSAGE_KINDS = (
    ("cq", "CQ"),                    # CQ MYCALL LOC4
    ("reply", "Reply (call + locator)"),  # DXCALL MYCALL LOC4
    ("report", "Report"),            # DXCALL MYCALL -12
    ("r_report", "R + report"),      # DXCALL MYCALL R-12
    ("rrr", "RRR"),
    ("rr73", "RR73"),
    ("73", "73"),
    ("free", "Free text"),           # up to 13 characters
)
FREE_TEXT_MAX = 13


def format_report(db: int) -> str:
    """WSJT-X style signal report: always signed, two digits (-09, +05)."""
    db = max(-30, min(30, int(db)))
    return f"{db:+03d}"


def compose(kind: str, my_call: str, my_locator: str = "", dx_call: str = "", report_db: int = -10,
            free_text: str = "") -> str:
    """Build a standard FT8 message text; returns "" if a required field is missing."""
    my = my_call.strip().upper()
    dx = dx_call.strip().upper()
    loc = my_locator.strip().upper()[:4]
    if kind == "free":
        return free_text.strip().upper()[:FREE_TEXT_MAX]
    if not my:
        return ""
    if kind == "cq":
        return f"CQ {my} {loc}".strip()
    if not dx:
        return ""
    tail = {"reply": loc, "report": format_report(report_db), "r_report": "R" + format_report(report_db),
            "rrr": "RRR", "rr73": "RR73", "73": "73"}.get(kind)
    if tail is None:
        raise ValueError(f"unknown FT8 message kind {kind!r}")
    return f"{dx} {my} {tail}".strip()


def can_encode(text: str) -> bool:
    return bool(text.strip()) and ft8_ctypes.FT8_AVAILABLE and ft8_ctypes.encode_message(text.strip().upper()) is not None


def next_slot_start(now: float, parity: str = "any", slot_s: float = FT8_SLOT_TIME) -> float:
    """UTC epoch time of the next slot boundary matching `parity` ("even": :00/:30, "odd": :15/:45,
    "any"), strictly after `now`."""
    t = (int(now // slot_s) + 1) * slot_s
    while parity != "any" and (int(t // slot_s) % 2 == 0) != (parity == "even"):
        t += slot_s
    return t


def current_slot_start(now: float, slot_s: float = FT8_SLOT_TIME) -> float:
    return (now // slot_s) * slot_s


def slot_parity(t: float, slot_s: float = FT8_SLOT_TIME) -> str:
    return "even" if int(t // slot_s) % 2 == 0 else "odd"


def plan_transmission(now: float, parity: str = "any", key_early_s: float = 3.0, start_in_slot_s: float = 0.5,
                      late_max_s: float = 1.0, slot_s: float = FT8_SLOT_TIME):
    """-> (key_at, start_at): when to call key_ptt() and when the signal should start. The flowgraph
    pads the signal with silence up to start_at at the moment it swaps the source in (see
    PlutoTxFlowgraph.ft8_start_at), so keying just has to happen early enough (key_early_s covers the
    graph lock, which varies from 0.05 to ~2.5 s on a Pluto). A matching slot whose nominal start is
    at most late_max_s past still gets used (start as soon as possible: a larger DT at the receiver);
    otherwise the next slot of the wanted parity ("even" = :00/:30, "odd" = :15/:45, "any")."""
    cur = current_slot_start(now, slot_s)
    ok = parity == "any" or slot_parity(cur, slot_s) == parity
    nominal = cur + start_in_slot_s
    if ok and now <= nominal + late_max_s:
        start = max(nominal, now)
        return max(now, start - key_early_s), start
    slot = next_slot_start(now, parity, slot_s)
    start = slot + start_in_slot_s
    return max(now, start - key_early_s), start
