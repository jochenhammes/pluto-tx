import numpy as np

from web_trx import protocol


def test_spectrum_row_roundtrip():
    row = np.linspace(-100.0, -20.0, 2048, dtype=np.float32)
    frame = protocol.encode_spectrum_row(row, center_hz=432_500_000.0, span_hz=2_500_000.0, generation=7)
    decoded = protocol.decode_spectrum_row(frame)
    assert decoded["generation"] == 7
    assert decoded["center_hz"] == 432_500_000.0
    assert decoded["span_hz"] == 2_500_000.0
    np.testing.assert_allclose(decoded["row"], row, atol=1e-4)


def test_tx_audio_roundtrip():
    pcm = bytes(range(0, 200, 2)) * 4
    frame = bytes([protocol.BinaryFrameType.TX_AUDIO]) + (48_000).to_bytes(4, "little") + pcm
    sample_rate_hz, decoded_pcm = protocol.decode_tx_audio(frame)
    assert sample_rate_hz == 48_000
    assert decoded_pcm == pcm


def test_peek_frame_type():
    frame = protocol.encode_rx_audio(b"\x00\x01", sample_rate_hz=8000)
    assert protocol.peek_frame_type(frame) == protocol.BinaryFrameType.RX_AUDIO
