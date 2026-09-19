"""POCSAG transmit audio: NRZ baseband for direct FM (see pocsag_codec.py for the protocol).

encode_message() returns the 48 kHz float stream (+1 / -1, logic 1 = -1 = lower frequency) that the
flowgraph's POCSAG branch resamples and feeds to a frequency modulator with sensitivity for
+-POCSAG_DEVIATION_HZ. The NRZ is low-pass shaped (zero-phase, edge-padded)
(Gaussian, BT 0.8) to keep the occupied bandwidth inside a 25 kHz channel.
"""
import numpy as np

from . import config
from . import pocsag_codec as codec

_GAUSS_BT = 0.8  # 3 dB bandwidth / baud; a Gaussian has no overshoot, so the peak deviation stays +-4.5 kHz
_EDGE_PAD_S = 0.02


def message_bits(ric, function, kind, text, charset="ascii"):
    """kind: 'alpha', 'numeric' or 'tone' (address only, text ignored)."""
    if kind not in ("alpha", "numeric", "tone"):
        raise ValueError(f"unknown POCSAG message kind {kind!r}")
    payload = None if kind == "tone" else codec.text_to_bits(text, kind, charset)
    return codec.build_transmission(ric, function, payload)


def encode_message(ric, function, kind, text, baud, charset="ascii", sample_rate=config.AUDIO_RATE):
    """-> (audio float32, duration_s)."""
    if baud not in codec.BAUD_RATES:
        raise ValueError(f"baud rate must be one of {codec.BAUD_RATES}")
    bits = np.array(message_bits(ric, function, kind, text, charset), dtype=np.float64)
    n = int(np.ceil(len(bits) * sample_rate / baud))
    nrz = 1.0 - 2.0 * bits[np.minimum((np.arange(n) * baud) // sample_rate, len(bits) - 1).astype(int)]
    pad = int(_EDGE_PAD_S * sample_rate)
    padded = np.concatenate([np.full(pad, nrz[0]), nrz, np.full(pad, nrz[-1])])
    freqs = np.fft.rfftfreq(len(padded), 1.0 / sample_rate)
    response = np.exp(-np.log(2) / 2 * (freqs / (_GAUSS_BT * baud)) ** 2)
    shaped = np.fft.irfft(np.fft.rfft(padded) * response, len(padded))[pad:pad + n]
    return shaped.astype(np.float32), n / sample_rate


def estimate_duration(ric, function, kind, text, baud, charset="ascii"):
    return len(message_bits(ric, function, kind, text, charset)) / baud
