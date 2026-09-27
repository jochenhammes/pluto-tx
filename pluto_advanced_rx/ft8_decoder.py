"""FT8 slot decoder: one 15 s slot of 12 kHz audio in, a list of decoded messages out.

GNU-Radio-independent (plain numpy), so it is testable with WAV files and usable from the RX app,
the CLI and offline tools alike. Two backends:

- "jt9": WSJT-X's own decoder, run as a subprocess on a WAV of the slot (`jt9 -8`, from the `wsjtx`
  apt package). Measured on ft8_lib's 66 reference recordings and on live 20 m audio from a KiwiSDR,
  it finds ~1.5x as many messages as ft8_lib (coherent multi-symbol demodulation, OSD, a-priori
  decoding, signal subtraction), in ~1-2 s per busy slot. Used whenever it is installed.
- "ft8lib": kgoba/ft8_lib through ft8_ctypes (built by install-ft8.sh). Always available once that
  is installed; decodes ~64 % of what jt9 finds at SNR >= -15 dB. ft8_lib's own demo leaves out a
  persistent callsign hash table, duplicate suppression and an SNR estimate, which this adds.
"""
import ctypes
import os
import re
import shutil
import subprocess
import tempfile
import wave
from dataclasses import dataclass

import numpy as np

from . import ft8_ctypes as fc

SAMPLE_RATE = 12000
SLOT_S = fc.FT8_SLOT_TIME
SLOT_SAMPLES = int(SLOT_S * SAMPLE_RATE)
NSPS = int(round(fc.FT8_SYMBOL_PERIOD * SAMPLE_RATE))   # 1920 samples per symbol
# FT8 signals start 0.5 s into the slot; ft8_lib reports time from the start of the audio.
NOMINAL_START_S = 0.5
# 10*log10(2500 Hz / 6.25 Hz): noise in one tone bin vs WSJT-X's 2500 Hz SNR reference bandwidth.
_BW_CORRECTION_DB = 10 * np.log10(2500.0 / fc.FT8_TONE_SPACING_HZ)
JT9 = shutil.which("jt9")
_JT9_LINE = re.compile(r"^\d{4,6}\s+(-?\d+)\s+(-?[\d.]+)\s+(\d+)\s+~\s+(.*?)\s*$")
# jt9's built-in "my call" when none is given: its a-priori decoding then hypothesises messages to
# this (fictional) call, which occasionally "decode" out of noise -- such decodes are dropped.
_JT9_DEFAULT_CALL = "K1ABC"


@dataclass
class Ft8Decode:
    text: str
    snr_db: float
    dt_s: float       # time offset vs. the nominal 0.5 s start, as WSJT-X shows it
    freq_hz: float    # audio frequency of tone 0


def sender_call(text: str):
    """The transmitting station's callsign of a standard message ("CQ [DX|POTA|..] CALL LOC" -> CALL,
    "TO FROM ..." -> FROM), without hash brackets; None for free text or anything unrecognised."""
    t = text.upper().split()
    if len(t) < 2:
        return None
    if t[0] == "CQ":
        call = t[2] if len(t) >= 4 else t[1]
    else:
        call = t[1]
    call = call.strip("<>")
    if call in ("...", "R", "RRR", "RR73", "73") or not any(ch.isdigit() for ch in call):
        return None
    return call


def parse_jt9_output(out: str, my_call: str = "", f_min: float = 0.0, f_max: float = 1e9):
    """Decode lines of `jt9 -8` -> [Ft8Decode]. Drops low-confidence decodes (jt9's trailing "?") and,
    when no own call was given, a-priori artefacts addressed to jt9's placeholder call K1ABC."""
    res = []
    for line in out.splitlines():
        m = _JT9_LINE.match(line)
        if not m:
            continue
        words = m.group(4).split()
        doubtful = False
        while words and (words[-1] == "?" or re.fullmatch(r"a\d", words[-1])):
            doubtful |= words.pop() == "?"     # "?" = low-confidence decode; aN = a-priori type
        if doubtful or not words or (not my_call and _JT9_DEFAULT_CALL in words):
            continue
        f = float(m.group(3))
        if f_min <= f <= f_max:
            res.append(Ft8Decode(" ".join(words), float(m.group(1)), float(m.group(2)), f))
    return res


def available_backends():
    out = []
    if JT9:
        out.append("jt9")
    if fc.FT8_AVAILABLE:
        out.append("ft8lib")
    return out


class CallsignHashTable:
    """22-bit callsign hashes -> callsign, fed by every decoded message (ft8_lib calls save_hash for
    each full callsign it unpacks) and consulted when a message only carries a hash."""

    def __init__(self, max_entries=2000):
        self._calls = {}          # hash22 -> callsign, oldest first
        self._max = max_entries
        self._cb = None

    def save(self, callsign: str, hash22: int):
        self._calls.pop(hash22, None)
        self._calls[hash22] = callsign
        while len(self._calls) > self._max:
            self._calls.pop(next(iter(self._calls)))

    def lookup(self, hash_type: int, h: int):
        shift = {0: 0, 1: 10, 2: 12}[hash_type]         # 22 / 12 / 10 bit hash
        for h22, call in self._calls.items():
            if (h22 >> shift) == h:
                return call
        return None

    def interface(self):
        def lookup(hash_type, h, out):
            call = self.lookup(hash_type, h)
            if call is None:
                return False
            raw = call.encode("ascii")[:11] + b"\0"
            ctypes.memmove(out, raw, len(raw))
            return True

        def save(callsign, h):
            try:
                self.save(callsign.decode("ascii"), h)
            except Exception:
                pass

        # keep the ctypes callback objects alive as long as the interface struct
        self._cb = (fc._LOOKUP_HASH_FN(lookup), fc._SAVE_HASH_FN(save))
        return fc.FtxCallsignHashInterface(*self._cb)


def _slot_audio(audio):
    x = np.zeros(SLOT_SAMPLES, dtype=np.float32)
    a = np.asarray(audio, dtype=np.float32)[:SLOT_SAMPLES]
    x[:len(a)] = a
    peak = float(np.max(np.abs(x)))
    return x / peak if peak > 0 else x


class Ft8SlotDecoder:
    """backend: "auto" (jt9 if installed, else ft8lib), "jt9" or "ft8lib".
    Not thread-safe; one instance per decoding thread."""

    def __init__(self, backend="auto", f_min=100.0, f_max=3100.0, depth=2, my_call="", my_grid=""):
        if backend == "auto":
            backends = available_backends()
            if not backends:
                raise RuntimeError("no FT8 decoder: install WSJT-X (jt9) or run install-ft8.sh")
            backend = backends[0]
        if backend == "jt9" and not JT9:
            raise RuntimeError("jt9 (WSJT-X) not found")
        if backend == "ft8lib" and not fc.FT8_AVAILABLE:
            raise RuntimeError("ft8_lib not available (see install-ft8.sh)")
        self.backend = backend
        self.f_min, self.f_max = f_min, f_max
        self.depth = depth
        # jt9 only: messages to my_call are decoded with a-priori knowledge (more sensitive for replies)
        self.my_call = my_call.strip().upper()
        self.my_grid = my_grid.strip().upper()[:4]
        self.hashes = CallsignHashTable()
        self._tmp = None

    def decode(self, audio: np.ndarray):
        """audio: float samples at 12 kHz, starting at the UTC slot boundary (up to 15 s; shorter is
        zero-padded). Returns decodes sorted by frequency."""
        x = _slot_audio(audio)
        res = self._decode_jt9(x) if self.backend == "jt9" else self._decode_ft8lib(x)
        return sorted(res, key=lambda d: d.freq_hz)

    # --- jt9 -------------------------------------------------------------------------------------
    def _decode_jt9(self, x):
        if self._tmp is None:
            self._tmp = tempfile.mkdtemp(prefix="ft8_jt9_")
        path = os.path.join(self._tmp, "slot.wav")
        with wave.open(path, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(SAMPLE_RATE)
            w.writeframes((x * 20000).astype("<i2").tobytes())
        cmd = [JT9, "-8", "-d", str(self.depth), "-L", str(int(self.f_min)), "-H", str(int(self.f_max)),
               "-a", self._tmp, "-t", self._tmp]
        if self.my_call:
            cmd += ["-c", self.my_call] + (["-G", self.my_grid] if self.my_grid else [])
        out = subprocess.run(cmd + [path], cwd=self._tmp, capture_output=True, text=True, timeout=30).stdout
        return parse_jt9_output(out, self.my_call, self.f_min, self.f_max)

    # --- ft8_lib ---------------------------------------------------------------------------------
    def _decode_ft8lib(self, x, num_candidates=140, min_score=10, ldpc_iterations=25):
        mon = fc.Ft8Monitor(SAMPLE_RATE, self.f_min, self.f_max)
        try:
            for k in range(len(x) // NSPS):
                mon.process(x[k * NSPS:(k + 1) * NSPS])
            m = mon._m
            wf = m.wf
            mag = np.ctypeslib.as_array(wf.mag, shape=(wf.max_blocks * wf.block_stride,))
            noise_db = float(np.median(mag[:wf.num_blocks * wf.block_stride])) / 2 - 120
            heap = (fc.FtxCandidate * num_candidates)()
            n = fc._lib.ftx_find_candidates(ctypes.byref(wf), num_candidates, heap, min_score)
            hash_if = self.hashes.interface()
            seen, res = set(), []
            for i in range(n):
                cand = heap[i]
                msg = fc.FtxMessage()
                status = fc.FtxDecodeStatus()
                if not fc._lib.ftx_decode_candidate(ctypes.byref(wf), ctypes.byref(cand), ldpc_iterations,
                                                    ctypes.byref(msg), ctypes.byref(status)):
                    continue
                key = bytes(msg.payload)
                if key in seen:
                    continue
                out = ctypes.create_string_buffer(fc.FTX_MAX_MESSAGE_LENGTH + 1)
                offs = fc.FtxMessageOffsets()
                if fc._lib.ftx_message_decode(ctypes.byref(msg), ctypes.byref(hash_if), out,
                                              ctypes.byref(offs)) != fc.FTX_MESSAGE_RC_OK:
                    continue
                seen.add(key)
                tones = (ctypes.c_uint8 * fc.FT8_NN)()
                fc._lib.ft8_encode(msg.payload, tones)
                freq = (m.min_bin + cand.freq_offset + cand.freq_sub / wf.freq_osr) / m.symbol_period
                t0 = (cand.time_offset + cand.time_sub / wf.time_osr) * m.symbol_period
                res.append(Ft8Decode(out.value.decode("ascii", "replace"),
                                     self._snr(mag, wf, cand, tones, noise_db), t0 - NOMINAL_START_S, freq))
            return res
        finally:
            mon.close()

    @staticmethod
    def _snr(mag, wf, cand, tones, noise_db):
        """Mean power in the decoded tone bins over all symbols, vs. the median waterfall level (the
        noise floor per tone bin), scaled to 2500 Hz. The waterfall stores 2*dB+240 as uint8."""
        base = ((cand.time_offset * wf.time_osr + cand.time_sub) * wf.freq_osr + cand.freq_sub) * wf.num_bins \
            + cand.freq_offset
        vals = [mag[base + k * wf.block_stride + int(t)] for k, t in enumerate(tones)
                if 0 <= cand.time_offset + k < wf.num_blocks]
        if not vals:
            return -30.0
        sig_db = 10 * np.log10(np.mean(10 ** ((np.asarray(vals, float) / 2 - 120) / 10)))
        s_over_n = 10 ** ((sig_db - noise_db) / 10) - 1
        return float(np.clip(10 * np.log10(max(s_over_n, 1e-3)) - _BW_CORRECTION_DB, -30, 40))

    def close(self):
        if self._tmp:
            shutil.rmtree(self._tmp, ignore_errors=True)
            self._tmp = None

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass
