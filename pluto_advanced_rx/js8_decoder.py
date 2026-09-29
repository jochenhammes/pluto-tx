"""JS8 slot decoder: one period of 12 kHz audio (sample 0 = UTC period start) in, decoded frames out.

GNU-Radio-independent (numpy), like ft8_decoder.py. Two backends:

- "js8": JS8Call's own decoder -- tools/js8ref (built from the unmodified JS8Call v2.5.2 sources by
  install-js8.sh) run as a subprocess on a WAV of the slot. Preferred whenever it is built.
- "own": a numpy port of ft8_lib's method (kgoba/ft8_lib ft8/decode.c + ft8/ldpc.c) with the JS8
  parameters from docs/js8/SPEC.md: a waterfall with 2x time and 2x frequency oversampling in 0.5 dB
  steps, candidate search on the three Costas arrays (same neighbour score as ft8_sync_score), max-log
  bit likelihoods of the 58 data symbols (no Gray code; parity symbols first), variance normalisation
  sqrt(24/var), belief propagation on JS8's own Tanner graph (LDPC_NM/LDPC_MN, 30 iterations as
  JS8.cpp's BP_MAX_ITERATIONS), CRC12, duplicate suppression, SNR estimate. Differences to ft8_lib: the
  waterfall frames are centred on the symbol (so the time offset is the symbol start directly) and a
  decode must also leave at most MAX_HARD_ERRORS hard-decision disagreements (JS8.cpp:1551 drops decodes
  with sync < 2 and more than 35).

Both return Js8Decode with the displayed text built by js8_message.decode_frame() (JS8Call's
DecodedText), so the two backends are interchangeable.
"""
import json
import os
import shutil
import subprocess
import tempfile
import wave
from dataclasses import dataclass

import numpy as np

from pluto_tx import js8_message, js8_phy
from pluto_tx import js8_tables as T

SAMPLE_RATE = 12000
TIME_OSR = 2
FREQ_OSR = 2
MIN_SCORE = 10                  # ft8_lib kMin_score (0.5 dB units)
MAX_CANDIDATES = 140            # ft8_lib kMax_candidates
BP_ITERATIONS = 30              # JS8.cpp BP_MAX_ITERATIONS
MAX_HARD_ERRORS = 35            # JS8.cpp:1551
DT_BEFORE_S = 1.5               # search window around the nominal start (period + start delay)
DT_AFTER_S = 2.5
SUBMODE_MASK = {js8_phy.NORMAL: 1, js8_phy.FAST: 2, js8_phy.TURBO: 4, js8_phy.SLOW: 8}
_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JS8REF = os.environ.get("JS8REF", os.path.join(_REPO, "js8call", "build-js8ref", "js8ref"))

_DATA_POS = np.concatenate([np.arange(7, 36), np.arange(43, 72)])
_COSTAS_POS = (0, 36, 72)


@dataclass
class Js8Decode:
    frame: str
    flags: int
    submode: int
    snr_db: float
    dt_s: float         # vs. the nominal start (period start + start delay), as JS8Call shows it
    freq_hz: float      # audio frequency of tone 0
    text: str           # what JS8Call displays for this frame (DecodedText)
    frame_type: int

    @property
    def is_first(self):
        return bool(self.flags & js8_phy.FLAG_FIRST)

    @property
    def is_last(self):
        return bool(self.flags & js8_phy.FLAG_LAST)


def available_backends():
    out = []
    if os.access(JS8REF, os.X_OK):
        out.append("js8")
    out.append("own")
    return out


def _make_decode(frame, flags, submode, snr, dt, freq):
    d = js8_message.decode_frame(frame, flags, submode)
    return Js8Decode(frame, flags, submode, round(float(snr), 1), round(float(dt), 2), round(float(freq), 1),
                     d["message"], d["frame_type"])


def _slot_audio(audio, submode):
    n = js8_phy.SUBMODES[submode]["period_s"] * SAMPLE_RATE
    x = np.zeros(n, dtype=np.float64)
    a = np.asarray(audio, dtype=np.float64)[:n]
    x[:len(a)] = a
    x -= x.mean() if len(a) else 0.0
    peak = float(np.max(np.abs(x)))
    return x / peak if peak > 0 else x


# --- LDPC ----------------------------------------------------------------------------------------------------

def _graph():
    nm = T.LDPC_NM
    m_checks = len(nm)
    width = max(len(r) for r in nm)
    nm_pad = np.full((m_checks, width), -1, dtype=np.int64)
    for m, bits in enumerate(nm):
        nm_pad[m, :len(bits)] = bits
    mn = np.array(T.LDPC_MN, dtype=np.int64)
    # edge (check m, slot j) <-> (bit n, slot i) with mn[n, i] == m
    edge_m, edge_j, edge_n, edge_i = [], [], [], []
    for m, bits in enumerate(nm):
        for j, n in enumerate(bits):
            i = int(np.where(mn[n] == m)[0][0])
            edge_m.append(m)
            edge_j.append(j)
            edge_n.append(n)
            edge_i.append(i)
    return nm_pad, mn, tuple(np.array(a) for a in (edge_m, edge_j, edge_n, edge_i))


_NM, _MN, (_EM, _EJ, _EN, _EI) = _graph()
_NM_VALID = _NM >= 0


def _parity_errors(plain):
    bits = np.where(_NM_VALID, plain[np.maximum(_NM, 0)], 0)
    return int(np.count_nonzero(bits.sum(axis=1) % 2))


def bp_decode(llr, max_iters=BP_ITERATIONS):
    """ft8_lib bp_decode() on JS8's graph. llr > 0 means bit 1. -> (174 hard bits, parity errors)."""
    llr = np.asarray(llr, dtype=np.float64)
    tov = np.zeros((len(llr), 3))
    toc = np.ones(_NM.shape)
    best, best_err = None, len(_NM)
    for _ in range(max_iters):
        plain = ((llr + tov.sum(axis=1)) > 0).astype(np.uint8)
        if not plain.any():
            break
        err = _parity_errors(plain)
        if err < best_err:
            best, best_err = plain, err
            if err == 0:
                break
        t_nm = llr[_EN] + tov[_EN].sum(axis=1) - tov[_EN, _EI]
        toc[:] = 1.0
        toc[_EM, _EJ] = np.tanh(-t_nm / 2)
        others = np.empty_like(toc)
        for j in range(toc.shape[1]):
            mask = np.ones(toc.shape[1], dtype=bool)
            mask[j] = False
            others[:, j] = np.prod(toc[:, mask], axis=1)
        t_mn = np.clip(others[_EM, _EJ], -0.9999999, 0.9999999)
        tov[_EN, _EI] = -2 * np.arctanh(t_mn)
    if best is None:
        best = np.zeros(len(llr), dtype=np.uint8)
    return best, best_err


# --- waterfall and candidates ----------------------------------------------------------------------------------

class _Waterfall:
    """mag[block, time_sub, freq_sub, bin] in 0.5 dB steps (2*dB + 240, like ft8_lib); frame (b, t) is a
    Hann window of 2 symbols centred on the middle of symbol b shifted by t/TIME_OSR symbols."""

    def __init__(self, x, nsps, f_min, f_max):
        self.nsps = nsps
        spacing = SAMPLE_RATE / nsps
        self.spacing = spacing
        nfft = nsps * FREQ_OSR
        self.min_bin = int(f_min // spacing)
        self.num_bins = int(np.ceil(f_max / spacing)) - self.min_bin
        self.num_blocks = int(np.ceil(len(x) / nsps))
        n_frames = self.num_blocks * TIME_OSR
        sub = nsps // TIME_OSR
        pad = nfft
        xp = np.concatenate([np.zeros(pad), x, np.zeros(pad + nfft)])
        starts = pad + np.arange(n_frames) * sub + nsps // 2 - nfft // 2
        idx = starts[:, None] + np.arange(nfft)[None, :]
        window = (2.0 / nfft) * np.sin(np.pi * np.arange(nfft) / nfft) ** 2
        spec = np.fft.rfft(xp[idx] * window, axis=1)
        p = spec.real ** 2 + spec.imag ** 2
        db = 10 * np.log10(1e-12 + p)
        q = np.clip(np.floor(2 * db + 240), 0, 255)
        bins = (self.min_bin + np.arange(self.num_bins))[None, :] * FREQ_OSR + np.arange(FREQ_OSR)[:, None]
        bins = np.minimum(bins, q.shape[1] - 1)
        # (frames, freq_sub, bin) -> (block, time_sub, freq_sub, bin)
        self.mag = q[:, bins].reshape(self.num_blocks, TIME_OSR, FREQ_OSR, self.num_bins)
        self.noise_db = float(np.median(self.mag)) / 2 - 120


def _sync_scores(wf, costas, o_range):
    """ft8_sync_score for every (time_offset in o_range, time_sub, freq_sub, freq_offset) at once ->
    array (len(o_range), TIME_OSR, FREQ_OSR, num_bins - 7)."""
    nb, nf = wf.num_blocks, wf.num_bins - 7
    o = np.asarray(o_range)
    m = wf.mag.astype(np.float64)
    out = np.zeros((len(o), TIME_OSR, FREQ_OSR, nf))
    cnt = np.zeros_like(out)
    fcols = np.arange(nf)
    for pos, arr in zip(_COSTAS_POS, costas):
        for k, sm in enumerate(arr):
            b = o + pos + k
            valid = (b >= 0) & (b < nb)
            if not valid.any():
                continue
            bv = b[valid]
            c = m[bv][:, :, :, fcols + sm]
            terms = []
            if sm > 0:
                terms.append((c - m[bv][:, :, :, fcols + sm - 1], np.ones(len(bv), bool)))
            if sm < 7:
                terms.append((c - m[bv][:, :, :, fcols + sm + 1], np.ones(len(bv), bool)))
            if k > 0:
                ok = bv > 0
                prev = m[np.maximum(bv - 1, 0)][:, :, :, fcols + sm]
                terms.append((c - prev, ok))
            if k + 1 < 7:
                ok = bv + 1 < nb
                nxt = m[np.minimum(bv + 1, nb - 1)][:, :, :, fcols + sm]
                terms.append((c - nxt, ok))
            for val, ok in terms:
                w = ok[:, None, None, None].astype(np.float64)
                out[valid] += val * w
                cnt[valid] += w
    with np.errstate(invalid="ignore", divide="ignore"):
        return np.where(cnt > 0, out / np.maximum(cnt, 1), -1e9)


def _likelihoods(wf, o, t, fs, f):
    """Max-log likelihoods of the 174 codeword bits (ft8_extract_symbol without the Gray map)."""
    llr = np.zeros(174)
    blocks = o + _DATA_POS
    ok = (blocks >= 0) & (blocks < wf.num_blocks)
    s2 = wf.mag[np.clip(blocks, 0, wf.num_blocks - 1), t, fs, f:f + 8].astype(np.float64) / 2 - 120
    one = [(4, 5, 6, 7), (2, 3, 6, 7), (1, 3, 5, 7)]
    zero = [(0, 1, 2, 3), (0, 1, 4, 5), (0, 2, 4, 6)]
    for bit in range(3):
        v = s2[:, one[bit]].max(axis=1) - s2[:, zero[bit]].max(axis=1)
        llr[bit::3] = np.where(ok, v, 0.0)
    var = llr.var()
    if var > 0:
        llr *= np.sqrt(24.0 / var)
    return llr


def decode_own(audio, submode, f_min=200.0, f_max=3000.0, max_candidates=MAX_CANDIDATES):
    info = js8_phy.submode_info(submode)
    nsps = info["symbol_samples"]
    sym = info["symbol_period_s"]
    delay = info["start_delay_ms"] / 1000
    x = _slot_audio(audio, submode)
    wf = _Waterfall(x, nsps, f_min, f_max)
    o_min = int(np.floor((delay - DT_BEFORE_S) / sym))
    o_max = int(np.ceil((delay + DT_AFTER_S) / sym))
    o_range = np.arange(o_min, o_max + 1)
    scores = _sync_scores(wf, js8_phy.costas(submode), o_range)
    flat = scores.reshape(-1)
    order = np.argsort(flat)[::-1]
    order = order[flat[order] >= MIN_SCORE][:max_candidates]
    seen, res = {}, []
    for idx in order:
        oi, t, fs, f = np.unravel_index(idx, scores.shape)
        o = int(o_range[oi])
        llr = _likelihoods(wf, o, t, fs, f)
        cw, err = bp_decode(llr)
        if err != 0:
            continue
        hard_errors = int(np.count_nonzero((llr > 0) != (cw == 1)))
        if hard_errors > MAX_HARD_ERRORS:
            continue
        got = js8_phy.bits_to_frame(cw[js8_phy.K:])
        if got is None:
            continue
        frame, flags = got
        freq = (wf.min_bin + f) * wf.spacing + fs * wf.spacing / FREQ_OSR
        dt = (o + t / TIME_OSR) * sym - delay
        key = (frame, flags)
        if key in seen:
            continue
        seen[key] = True
        tones = js8_phy.encode(frame, flags, submode)
        res.append(_make_decode(frame, flags, submode, _snr(wf, o, t, fs, f, tones), dt, freq))
    return sorted(res, key=lambda d: d.freq_hz)


# WSJT-style SNR in 2500 Hz: mean decoded-tone power vs. the median bin level. The median of an
# exponentially distributed power sits ln(2) (-1.6 dB) below its mean; the Hann window's noise bandwidth is
# 1.5 FFT bins = 0.75 tone spacings. The last term was calibrated against AWGN (tests/test_js8_rx.py).
_MEDIAN_BIAS_DB = 10 * np.log10(np.log(2))
_SNR_CAL_DB = 1.8


def _snr(wf, o, t, fs, f, tones):
    blocks = o + np.arange(js8_phy.NUM_SYMBOLS)
    ok = (blocks >= 0) & (blocks < wf.num_blocks)
    if not ok.any():
        return -30.0
    vals = wf.mag[blocks[ok], t, fs, f + np.asarray(tones)[ok]].astype(np.float64) / 2 - 120
    sig_db = 10 * np.log10(np.mean(10 ** (vals / 10)))
    noise_db = wf.noise_db - _MEDIAN_BIAS_DB
    s_over_n = 10 ** ((sig_db - noise_db) / 10) - 1
    enbw = 0.75 * wf.spacing
    return float(np.clip(10 * np.log10(max(s_over_n, 1e-3)) + 10 * np.log10(enbw / 2500.0) + _SNR_CAL_DB, -40, 40))


# --- js8ref backend ------------------------------------------------------------------------------------------

def parse_js8ref_output(out):
    res = []
    for line in out.splitlines():
        try:
            o = json.loads(line)
        except ValueError:
            continue
        if "frame" not in o:
            continue
        sm = int(o["submode"])
        d = js8_message.decode_frame(o["frame"], int(o["bits"]), sm)
        res.append(Js8Decode(o["frame"], int(o["bits"]), sm, float(o["snr"]), round(float(o["dt"]), 2),
                             round(float(o["freq"]), 1), d["message"], d["frame_type"]))
    return res


def write_wav(path, x):
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        w.writeframes((np.asarray(x) * 20000).astype("<i2").tobytes())


class Js8SlotDecoder:
    """backend: "auto" (js8 if built, else own), "js8" or "own". Not thread-safe; one per thread."""

    def __init__(self, backend="auto", f_min=200.0, f_max=3000.0):
        if backend == "auto":
            backend = available_backends()[0]
        if backend == "js8" and not os.access(JS8REF, os.X_OK):
            raise RuntimeError("js8ref not built (install-js8.sh)")
        if backend not in ("js8", "own"):
            raise ValueError(f"unknown JS8 decoder backend {backend!r}")
        self.backend = backend
        self.f_min, self.f_max = f_min, f_max
        self._tmp = None

    def decode(self, audio, submode):
        """audio: 12 kHz samples starting at the period boundary (shorter is zero-padded)."""
        if self.backend == "own":
            return decode_own(audio, submode, self.f_min, self.f_max)
        if self._tmp is None:
            self._tmp = tempfile.mkdtemp(prefix="js8ref_")
        path = os.path.join(self._tmp, "slot.wav")
        write_wav(path, _slot_audio(audio, submode))
        out = subprocess.run([JS8REF, "decode", path, str(SUBMODE_MASK[submode])], capture_output=True,
                             text=True, timeout=60).stdout
        res = [d for d in parse_js8ref_output(out) if self.f_min <= d.freq_hz <= self.f_max]
        return sorted(res, key=lambda d: d.freq_hz)

    def close(self):
        if self._tmp:
            shutil.rmtree(self._tmp, ignore_errors=True)
            self._tmp = None

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass
