"""Wire protocol shared between session backends and the WebSocket layer.

Text frames: JSON. Client -> server: {"request": "<name>", ...params}.
Server -> client: {"event": "<name>", ...fields} -- a superset of pluto-cli's
own --json event schema (see vendor/pluto-tx/pluto_cli/README.md section 6),
extended with session/control events (hello, scanned, connected, mode,
tuned, error) that pluto-cli has no equivalent for (it is a one-shot process,
Web-TRX is a long-lived session).

Binary frames: a 1-byte BinaryFrameType prefix followed by a type-specific
payload -- kept out of the JSON channel because spectrum rows and audio are
sent far more often than control events and gain nothing from JSON's
overhead.
"""
from __future__ import annotations

import enum
import struct

import numpy as np


class BinaryFrameType(enum.IntEnum):
    SPECTRUM_ROW = 0x01  # server -> client
    RX_AUDIO = 0x02  # server -> client
    TX_AUDIO = 0x03  # client -> server (microphone)


_SPECTRUM_HEADER = struct.Struct("<BIdd")  # type, generation, center_hz, span_hz
_AUDIO_HEADER = struct.Struct("<BI")  # type, sample_rate_hz


def encode_spectrum_row(row: np.ndarray, center_hz: float, span_hz: float, generation: int) -> bytes:
    """`row`: float32 dB magnitudes, ordered low-to-high frequency (already
    fftshift-ed/cropped by the source, see sim_backend.py / vendor
    FftProbe). `generation` lets a client detect dropped/duplicate rows
    without relying on frame arrival order."""
    header = _SPECTRUM_HEADER.pack(
        BinaryFrameType.SPECTRUM_ROW, generation & 0xFFFFFFFF, center_hz, span_hz
    )
    return header + row.astype("<f4").tobytes()


def decode_spectrum_row(frame: bytes) -> dict:
    frame_type, generation, center_hz, span_hz = _SPECTRUM_HEADER.unpack_from(frame)
    assert frame_type == BinaryFrameType.SPECTRUM_ROW
    row = np.frombuffer(frame, dtype="<f4", offset=_SPECTRUM_HEADER.size)
    return {"generation": generation, "center_hz": center_hz, "span_hz": span_hz, "row": row}


def encode_rx_audio(pcm16: bytes, sample_rate_hz: int) -> bytes:
    return _AUDIO_HEADER.pack(BinaryFrameType.RX_AUDIO, sample_rate_hz) + pcm16


def decode_tx_audio(frame: bytes) -> tuple[int, bytes]:
    """Caller has already checked frame[0] == BinaryFrameType.TX_AUDIO."""
    frame_type, sample_rate_hz = _AUDIO_HEADER.unpack_from(frame)
    assert frame_type == BinaryFrameType.TX_AUDIO
    return sample_rate_hz, frame[_AUDIO_HEADER.size:]


def peek_frame_type(frame: bytes) -> int:
    return frame[0]
