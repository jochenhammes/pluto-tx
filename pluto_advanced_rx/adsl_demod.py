"""ADS-L M-band burst demodulator: FM discriminator samples in, CRC-checked packets out. Numpy only, no GNU Radio.

Packets are about 5 ms long and arrive at most a few per second per aircraft, so instead of a continuously
running symbol clock this searches for the known preamble tail + sync pattern by FFT correlation and then
reads the whole frame at chip positions derived from that one timing estimate. The clock error over the
432 chips of a Traffic frame is negligible at any crystal tolerance. Works at any (also non-integer) ratio
of sample rate to chip rate, so no resampler is needed. Protocol parsing: pluto_tx/adsl_codec.py.

Each Manchester bit is decided from the difference of its two chips, so a constant frequency error (up to
what the channel filter lets through) has no effect at all and is reported rather than corrected.
"""
import threading
import time

import numpy as np

from pluto_tx import adsl_codec

CHIP_RATE = 100_000
DEVIATION_HZ = 50_000
WORKING_RATE_HZ = 400_000  # target discriminator rate, about 4 samples per chip; any nearby rate works
# Chosen by sweep (tests/adsl_measure.py): passes a +-50 kHz frequency error, rejects the neighbour 200 kHz away.
CHANNEL_CUTOFF_HZ = 110_000
CHANNEL_TRANS_HZ = 40_000
MAX_FRAME_LENGTH = 96  # longer frames use Reed-Solomon parity by recommendation, which is not decoded yet
DEFAULT_THRESHOLD = 0.5  # normalised correlation; only limits how many candidates reach the CRC
_PEAK_SEARCH_CHIPS = 8
_BLOCK_SAMPLES = 1 << 14
_MIN_ENERGY = 1e-9
_DUPLICATE_WINDOW_S = 0.25  # well above one search block's delay, far below a sender's packet spacing


def _manchester_chips(data: bytes):
    chips = []
    for byte in data:
        for i in range(7, -1, -1):
            chips += (0, 1) if byte >> i & 1 else (1, 0)
    return chips


# The minimum preamble every valid sender transmits, then the Manchester-coded sync word.
SYNC_PATTERN_CHIPS = [0, 1] * 5 + [1, 0, 0, 1, 1, 0, 0, 1] + _manchester_chips(adsl_codec.SYNC_BYTES)


class DuplicateFilter:
    """Passes each frame once across all channels. A strong sender also decodes on the neighbour channel, with its
    frequency error showing the true distance (seen on air at close range); both channels' threads call this."""

    def __init__(self, on_packet):
        self._on_packet = on_packet
        self._lock = threading.Lock()
        self._seen = {}
        self.duplicates = 0

    def __call__(self, info, now=None):
        now = time.monotonic() if now is None else now
        with self._lock:
            self._seen = {raw: t for raw, t in self._seen.items() if now - t < _DUPLICATE_WINDOW_S}
            if info["raw"] in self._seen:
                self.duplicates += 1
                return
            self._seen[info["raw"]] = now
        self._on_packet(info)


class AdslBurstDetector:
    """feed() discriminator samples (scaled so that +-1 = +-DEVIATION_HZ); on_frame(info) gets each packet that
    passes the CRC, with the adsl_codec.parse_frame() fields plus correlation and frequency_offset_hz."""

    def __init__(self, sample_rate, on_frame, threshold=DEFAULT_THRESHOLD):
        self.sample_rate = float(sample_rate)
        self.samples_per_chip = self.sample_rate / CHIP_RATE
        self._on_frame = on_frame
        self.threshold = threshold
        n_chips = len(SYNC_PATTERN_CHIPS)
        self._n = int(round(n_chips * self.samples_per_chip))
        positions = (np.arange(self._n) / self.samples_per_chip).astype(int)
        template = 2.0 * np.array(SYNC_PATTERN_CHIPS, dtype=np.float32)[np.minimum(positions, n_chips - 1)] - 1.0
        self._template = template - template.mean()
        self._template_energy = float(np.sum(self._template ** 2))
        self._frame_samples = int((1 + MAX_FRAME_LENGTH) * 16 * self.samples_per_chip) + 4
        self._search_samples = int(_PEAK_SEARCH_CHIPS * self.samples_per_chip) + 1
        self._overlap = self._n + self._frame_samples + self._search_samples
        self._nfft = 1
        while self._nfft < _BLOCK_SAMPLES + self._overlap + self._n:
            self._nfft *= 2
        self._template_fft = np.fft.rfft(self._template[::-1], self._nfft)
        self._chunks = []
        self._buffered = 0
        self._tail = np.zeros(0, dtype=np.float32)
        self._position = 0  # absolute index of the buffer's first sample
        self._skip_until = 0  # absolute index before which a new frame cannot start
        self.candidates = 0
        self.frames_ok = 0
        self.frames_bad = 0

    def feed(self, samples):
        self._chunks.append(np.array(samples, dtype=np.float32))
        self._buffered += len(samples)
        if self._buffered < _BLOCK_SAMPLES + self._overlap:
            return
        x = np.concatenate(self._chunks)
        start = 0
        while len(x) - start >= _BLOCK_SAMPLES + self._overlap:
            self._search(x[start:start + _BLOCK_SAMPLES + self._overlap], _BLOCK_SAMPLES)
            self._position += _BLOCK_SAMPLES
            start += _BLOCK_SAMPLES
        rest = x[start:]
        self._chunks = [rest]
        self._buffered = len(rest)

    def flush(self):
        """Search whatever is still buffered (end of a file or test; a live flowgraph never needs this)."""
        if not self._chunks:
            return
        x = np.concatenate(self._chunks)
        if len(x) > self._n:
            self._search(x, len(x) - self._n)
        self._chunks = []
        self._position += len(x)
        self._buffered = 0

    def _correlate(self, x):
        n = self._n
        spectrum = np.fft.rfft(x, self._nfft) * self._template_fft
        corr = np.fft.irfft(spectrum, self._nfft)[n - 1:len(x)]
        c1 = np.concatenate(((0.0,), np.cumsum(x, dtype=np.float64)))
        c2 = np.concatenate(((0.0,), np.cumsum(x.astype(np.float64) ** 2)))
        sum1 = c1[n:] - c1[:-n]
        energy = (c2[n:] - c2[:-n]) - sum1 * sum1 / n  # the local mean is a frequency error, not signal
        return corr / np.sqrt(np.maximum(energy, _MIN_ENERGY) * self._template_energy)

    def _search(self, x, search_len):
        if len(x) <= self._n:
            return
        ncc = self._correlate(x)
        limit = min(search_len, len(ncc))
        hits = np.flatnonzero(np.abs(ncc[:limit]) > self.threshold)
        next_allowed = self._skip_until - self._position
        for hit in hits:
            if hit < next_allowed:
                continue
            end = min(hit + self._search_samples, len(ncc))
            peak = hit + int(np.argmax(np.abs(ncc[hit:end])))
            self.candidates += 1
            length_chips = self._try_frame(x, ncc, peak)
            if length_chips:
                next_allowed = peak + self._n + int(length_chips * self.samples_per_chip)
                self._skip_until = self._position + next_allowed
            else:  # the search window already picked the strongest peak nearby: do not retry this same burst
                next_allowed = peak + self._search_samples

    def _peak_time(self, ncc, peak):
        if 0 < peak < len(ncc) - 1:
            a, b, c = abs(ncc[peak - 1]), abs(ncc[peak]), abs(ncc[peak + 1])
            denominator = a - 2 * b + c
            if denominator < 0:
                return peak + 0.5 * (a - c) / denominator
        return float(peak)

    def _read_bits(self, x, first_chip_time, n_bits, sign):
        """Soft Manchester decisions: mean of chip 2 minus mean of chip 1 of each bit."""
        spc = self.samples_per_chip
        edges = first_chip_time + np.arange(2 * n_bits + 1) * spc
        if edges[-1] + 1 >= len(x):
            return None
        lo = int(edges[0])
        hi = int(edges[-1]) + 2
        cumulative = np.concatenate(((0.0,), np.cumsum(x[lo:hi], dtype=np.float64)))
        at_edges = np.interp(edges - lo, np.arange(len(cumulative)), cumulative)
        chips = np.diff(at_edges) / spc
        return sign * (chips[1::2] - chips[0::2]), chips

    def _try_frame(self, x, ncc, peak):
        """Returns the frame's length in chips if a valid frame was read at this correlation peak."""
        time = self._peak_time(ncc, peak) + self._n
        sign = 1.0 if ncc[peak] > 0 else -1.0
        got = self._read_bits(x, time, 8, sign)
        if got is None:
            return 0
        length = int(np.packbits(got[0] > 0)[0])
        if not adsl_codec.MIN_LENGTH <= length <= MAX_FRAME_LENGTH:
            return 0
        n_bits = (1 + length) * 8
        got = self._read_bits(x, time, n_bits, sign)
        if got is None:
            return 0
        soft, chips = got
        info = adsl_codec.parse_frame(np.packbits(soft > 0).tobytes())
        if info is None:
            self.frames_bad += 1
            return 0
        self.frames_ok += 1
        info["correlation"] = round(abs(float(ncc[peak])), 3)
        info["frequency_offset_hz"] = round(float(np.mean(chips)) * DEVIATION_HZ)  # Manchester has no DC of its own
        info["inverted"] = sign < 0
        self._on_frame(info)
        return 2 * n_bits
