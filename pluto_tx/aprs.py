"""APRS transmit audio: an AX.25 frame as AFSK Bell 202 (1200 Bd, Mark 1200 Hz, Space 2200 Hz, NRZI, continuous phase). The
flowgraph's APRS branch feeds this to a frequency modulator or, on a sound card or AIOC, to the radio's microphone input."""
import numpy as np

from . import aprs_codec as codec
from . import config

BAUD = 1200
MARK_HZ = 1200.0
SPACE_HZ = 2200.0
FLAG_BITS = 8
PREAMBLE_S = 0.30  # flags ahead of the frame: transmitter and receiver squelch need time to open
TAIL_FLAGS = 4
AMPLITUDE = 0.9


def afsk_tones(bits) -> np.ndarray:
    """NRZI: a 0 changes the tone, a 1 keeps it. Starts on Mark."""
    tones = np.empty(len(bits), dtype=np.float64)
    freq = MARK_HZ
    for i, bit in enumerate(bits):
        if not bit:
            freq = SPACE_HZ if freq == MARK_HZ else MARK_HZ
        tones[i] = freq
    return tones


def encode_frame(frame: bytes, sample_rate=config.AUDIO_RATE, preamble_s=PREAMBLE_S) -> np.ndarray:
    """-> float32 audio with continuous phase, ending on silence-free tail flags."""
    flags = max(1, int(round(preamble_s * BAUD / FLAG_BITS)))
    tones = afsk_tones(codec.hdlc_bits(frame, preamble_flags=flags, tail_flags=TAIL_FLAGS))
    n = int(np.ceil(len(tones) * sample_rate / BAUD))
    freq = tones[np.minimum((np.arange(n) * BAUD / sample_rate).astype(int), len(tones) - 1)]
    return (AMPLITUDE * np.sin(2 * np.pi * np.cumsum(freq) / sample_rate)).astype(np.float32)


def airtime_s(frame: bytes, preamble_s=PREAMBLE_S) -> float:
    flags = max(1, int(round(preamble_s * BAUD / FLAG_BITS)))
    return len(codec.hdlc_bits(frame, preamble_flags=flags, tail_flags=TAIL_FLAGS)) / BAUD
