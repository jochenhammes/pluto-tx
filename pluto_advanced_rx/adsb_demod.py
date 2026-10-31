"""ADS-B burst demodulator: signal magnitude in, CRC-checked Extended Squitter messages out. Numpy only, no GNU Radio.

Each block's magnitude is first integrated onto a fixed grid of 0.25 us, so that the pulses sit at exact positions at any
sample rate (rounding them to whole samples made 2.4 and 2.5 MS/s measurably worse than 2 and 4). Below 2.4 MS/s a pulse
too often falls on two samples and neighbouring bits become ambiguous: 2.048 MS/s still fails at 20 dB SNR. On that
grid the four-pulse preamble is searched with array operations (no per-sample Python, the rate is millions per second),
then each bit is read as the energy of its first half against its second. A single flipped bit is corrected from the CRC
syndrome, but only for an address already heard intact: otherwise noise would invent aircraft.
"""
import numpy as np

from pluto_tx import adsb_codec
from pluto_tx.mode_s_crc import crc24

MESSAGE_BITS = 112
_PREAMBLE_US = 8.0
_FRAME_US = _PREAMBLE_US + MESSAGE_BITS
_BLOCK_SAMPLES = 1 << 17
_BINS_PER_US = 4
_HALF_BIT = 2  # bins
_PULSES = (0, 4, 14, 18)  # bins from the start of the preamble: 0, 1, 3.5 and 4.5 us
_GAPS = (2, 6, 8, 10, 12, 16, 20, 22, 24, 26, 28, 30)
_DATA = 32  # bins: 8 us
_MIN_PULSE_TO_GAP = 1.6  # the mean of the pulses against the mean of the gaps
_MIN_PULSE_TO_NOISE = 2.0  # against the block's noise floor (6 dB)
_PHASES = (0, -1, 1)  # bins, around the best preamble position
_KNOWN_ADDRESS_S = 120.0


def _single_bit_syndromes():
    table = {}
    zero = bytes(adsb_codec.MESSAGE_BYTES)
    for bit in range(5, MESSAGE_BITS):  # never the DF field, which decided to look here at all
        flipped = bytearray(zero)
        flipped[bit // 8] ^= 0x80 >> bit % 8
        table[crc24(bytes(flipped))] = bit
    return table


_SYNDROMES = _single_bit_syndromes()


class AdsbFrameDetector:
    """feed() signal magnitude (|IQ|); on_message(info) gets every valid message with adsb_codec.parse_message() fields
    plus "snr_db"."""

    def __init__(self, sample_rate, on_message):
        self.sample_rate = float(sample_rate)
        self.samples_per_us = self.sample_rate / 1e6
        self._on_message = on_message
        self._frame_samples = int(_FRAME_US * self.samples_per_us) + 2
        self._overlap = self._frame_samples + int(self.samples_per_us)
        self._chunks = []
        self._buffered = 0
        self._position = 0
        self._skip_until = 0
        self._known = {}
        self.candidates = 0
        self.messages_ok = 0
        self.corrected = 0
        self.crc_failures = 0

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
        if len(x) > self._frame_samples:
            self._search(x, len(x) - self._frame_samples)
        self._position += len(x)
        self._chunks = []
        self._buffered = 0

    def _search(self, x, search_len):
        cumulative = np.concatenate(((0.0,), np.cumsum(x, dtype=np.float64)))
        bins_per_sample = _BINS_PER_US / self.samples_per_us
        total = int((len(x) - 1) * bins_per_sample)
        grid = np.interp(np.arange(total) / bins_per_sample, np.arange(len(cumulative)), cumulative)
        energy = (grid[_HALF_BIT:] - grid[:-_HALF_BIT]).astype(np.float32)
        n = min(int(search_len * bins_per_sample), len(energy) - _GAPS[-1] - 1)
        if n <= 0:
            return
        pulses = sum(energy[o:o + n] for o in _PULSES) / len(_PULSES)
        gaps = sum(energy[o:o + n] for o in _GAPS) / len(_GAPS)
        noise = max(float(np.median(energy[::16])), 1e-20)
        weakest = energy[:n].copy()
        for o in _PULSES[1:]:
            weakest = np.minimum(weakest, energy[o:o + n])
        hits = np.flatnonzero((pulses > _MIN_PULSE_TO_GAP * gaps) & (pulses > _MIN_PULSE_TO_NOISE * noise)
                              & (weakest > 0.3 * pulses) & (weakest > gaps))
        if hits.size == 0:
            return
        scores = pulses - gaps
        next_allowed = (self._skip_until - self._position) * bins_per_sample
        for hit in hits:
            if hit < next_allowed:
                continue
            end = min(hit + _BINS_PER_US, n)
            peak = hit + int(np.argmax(scores[hit:end]))
            self.candidates += 1
            if self._try_frame(grid, peak, float(pulses[peak]), noise):
                next_allowed = peak + _FRAME_US * _BINS_PER_US
                self._skip_until = self._position + next_allowed / bins_per_sample
            else:
                next_allowed = peak + _BINS_PER_US

    def _try_frame(self, grid, peak, pulse_level, noise):
        wrong_format = True
        for phase in _PHASES:
            first = peak + phase + _DATA
            last = first + 2 * _HALF_BIT * MESSAGE_BITS
            if first < 0 or last >= len(grid):
                return False
            edges = grid[first:last + 1:_HALF_BIT]
            halves = np.diff(edges)
            message = np.packbits(halves[0::2] > halves[1::2]).tobytes()
            if adsb_codec.downlink_format(message) not in adsb_codec.ADSB_DOWNLINK_FORMATS:
                continue
            wrong_format = False
            if self._accept(message, pulse_level, noise):
                return True
        if not wrong_format:
            self.crc_failures += 1
        return False

    def _accept(self, message, pulse_level, noise):
        now = self._position / self.sample_rate
        syndrome = crc24(message)
        corrected = False
        if syndrome:
            bit = _SYNDROMES.get(syndrome)
            if bit is None:
                return False
            fixed = bytearray(message)
            fixed[bit // 8] ^= 0x80 >> bit % 8
            message = bytes(fixed)
            address = message[1:4]
            if now - self._known.get(address, -1e9) > _KNOWN_ADDRESS_S:
                return False
            corrected = True
        info = adsb_codec.parse_message(message)
        if info is None:
            return False
        if corrected:
            self.corrected += 1
        else:
            self._known[message[1:4]] = now
            if len(self._known) > 4096:
                self._known = {a: t for a, t in self._known.items() if now - t <= _KNOWN_ADDRESS_S}
        self.messages_ok += 1
        info["snr_db"] = round(20 * np.log10(max(pulse_level / noise, 1e-3)), 1)
        info["corrected"] = corrected
        info["raw"] = message.hex().upper()
        self._on_message(info)
        return True
