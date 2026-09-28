"""Jitter buffer between the browser microphone (bursty WebSocket frames)
and the TX flowgraph (a steady 48 kHz sample stream).

Pure Python/NumPy, no GNU Radio -- radio_backend.py's mic source block
drains it on GNU Radio's scheduler thread while the event loop fills it,
hence the lock. Kept separate so it is unit-testable anywhere.

Behaviour:
- Nothing is played out until PREBUFFER_S of audio has arrived (absorbs
  network jitter); after an underrun it waits for the prebuffer again
  instead of stuttering sample by sample.
- More than MAX_BUFFER_S queued means the network delivered a burst after
  a stall: the oldest audio is dropped so latency cannot keep growing.
- Frames at another sample rate (e.g. a 44.1 kHz AudioContext) are
  linearly resampled to the flowgraph rate.
"""
from __future__ import annotations

import threading

import numpy as np

PREBUFFER_S = 0.08
MAX_BUFFER_S = 0.4


class JitterBuffer:
    def __init__(self, rate_hz: int, prebuffer_s: float = PREBUFFER_S, max_buffer_s: float = MAX_BUFFER_S):
        self.rate_hz = rate_hz
        self._prebuffer = int(prebuffer_s * rate_hz)
        self._max = int(max_buffer_s * rate_hz)
        self._lock = threading.Lock()
        self._chunks: list[np.ndarray] = []
        self._len = 0
        self._playing = False
        self.underruns = 0
        self.dropped_samples = 0

    def clear(self) -> None:
        """Empties the buffer and resets the counters (called on every key)."""
        with self._lock:
            self._chunks.clear()
            self._len = 0
            self._playing = False
            self.underruns = 0
            self.dropped_samples = 0

    def push_pcm16(self, pcm16: bytes, rate_hz: int) -> None:
        samples = np.frombuffer(pcm16, dtype="<i2").astype(np.float32) / 32768.0
        if rate_hz != self.rate_hz and len(samples):
            n_out = round(len(samples) * self.rate_hz / rate_hz)
            samples = np.interp(
                np.arange(n_out) * (rate_hz / self.rate_hz), np.arange(len(samples)), samples,
            ).astype(np.float32)
        if not len(samples):
            return
        with self._lock:
            self._chunks.append(samples)
            self._len += len(samples)
            excess = self._len - self._max
            if excess > 0:
                self._drop_oldest(excess)
                self.dropped_samples += excess

    def pull(self, n: int) -> np.ndarray:
        """Always returns exactly n samples; silence where no audio is due."""
        out = np.zeros(n, dtype=np.float32)
        with self._lock:
            if not self._playing:
                if self._len < self._prebuffer:
                    return out
                self._playing = True
            filled = 0
            while filled < n and self._chunks:
                chunk = self._chunks[0]
                take = min(n - filled, len(chunk))
                out[filled:filled + take] = chunk[:take]
                filled += take
                if take == len(chunk):
                    self._chunks.pop(0)
                else:
                    self._chunks[0] = chunk[take:]
            self._len -= filled
            if filled < n:
                self._playing = False
                self.underruns += 1
        return out

    @property
    def buffered_s(self) -> float:
        with self._lock:
            return self._len / self.rate_hz

    def _drop_oldest(self, n: int) -> None:
        while n > 0 and self._chunks:
            chunk = self._chunks[0]
            if len(chunk) <= n:
                self._chunks.pop(0)
                n -= len(chunk)
                self._len -= len(chunk)
            else:
                self._chunks[0] = chunk[n:]
                self._len -= n
                n = 0
