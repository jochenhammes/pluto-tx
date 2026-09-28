"""JitterBuffer (tx_audio.py) -- pure NumPy, runs anywhere."""
import numpy as np

from web_trx.tx_audio import JitterBuffer

RATE = 48000


def pcm(n, value=1000):
    return np.full(n, value, dtype="<i2").tobytes()


def test_silence_until_prebuffer_filled():
    jb = JitterBuffer(RATE, prebuffer_s=0.08)
    jb.push_pcm16(pcm(RATE // 20), RATE)  # 50 ms < 80 ms prebuffer
    assert not jb.pull(480).any()
    jb.push_pcm16(pcm(RATE // 20), RATE)  # now 100 ms
    out = jb.pull(480)
    assert np.allclose(out, 1000 / 32768)


def test_underrun_pads_with_silence_and_rebuffers():
    jb = JitterBuffer(RATE, prebuffer_s=0.01)
    jb.push_pcm16(pcm(480), RATE)
    out = jb.pull(960)
    assert out[:480].all() and not out[480:].any()
    assert jb.underruns == 1
    jb.push_pcm16(pcm(100), RATE)  # below prebuffer again -> still silent
    assert not jb.pull(100).any()


def test_overflow_drops_oldest_audio():
    jb = JitterBuffer(RATE, prebuffer_s=0.01, max_buffer_s=0.1)
    jb.push_pcm16(pcm(RATE // 10, 1), RATE)
    jb.push_pcm16(pcm(RATE // 20, 2), RATE)
    assert abs(jb.buffered_s - 0.1) < 1e-6
    assert jb.dropped_samples == RATE // 20
    assert np.allclose(jb.pull(RATE // 20), 1 / 32768)  # the kept tail of the first chunk
    assert np.allclose(jb.pull(RATE // 20), 2 / 32768)


def test_resamples_other_rates():
    jb = JitterBuffer(RATE, prebuffer_s=0.0)
    jb.push_pcm16(pcm(4410), 44100)
    assert abs(jb.buffered_s - 0.1) < 1e-3
