"""A pure Python/numpy gr.sync_block implementing a standard feed-forward,
soft-knee dynamics processor (threshold/ratio/knee/attack/release/makeup
gain) -- used TWICE in flowgraph.py with different parameters, once as a
compressor (moderate ratio, slower release) and once as a smooth limiter
(high ratio, fast attack), instead of writing two separate classes.

Why a custom block: no GNU Radio block anywhere in gnuradio.analog,
gnuradio.filter, or gnuradio.blocks implements threshold/ratio/attack/
release dynamics processing (confirmed via dir(analog) -- only agc2_ff/
agc3_cc/agc_ff/feedforward_agc_cc exist, all single-time-constant automatic
LEVEL controls, not compressors). This follows the same pattern already
established in pluto_advanced_rx/fft_probe.py: a plain Python gr.sync_block,
parameters mutated under a threading.Lock, no GNU Radio scheduler/buffer
concerns -- and this block is actually simpler than fft_probe, because it's
strictly 1-in-1-out (in_sig=[np.float32], out_sig=[np.float32], like
blocks.multiply_const_ff) with no internal ring buffer or output_multiple
dependency at all, so every setter here is unconditionally safe to call at
runtime with zero risk of the scheduler-stall class of bug fft_probe.py's
docstring documents for its own (different) situation.

Algorithm: standard log-domain soft-knee gain computer (Giannoulis/Massberg/
Reiss "Digital Dynamic Range Compressor Design", 2012), smoothed on the
GAIN REDUCTION curve itself (not on the input level) via a branching
one-pole filter -- separate attack/release time constants depending on
whether more or less reduction is needed. Smoothing the gain-reduction
curve (rather than the level feeding a fixed curve) is what avoids clicks
right at the knee/threshold boundary.
"""
import threading

import numpy as np
from gnuradio import gr

_EPS = 1e-6


def _alpha_for(time_ms, sample_rate):
    """One-pole smoothing coefficient for a given time constant. time_ms==0
    means instant (alpha=0), guarding the divide-by-zero explicitly rather
    than relying on a huge-but-finite alpha."""
    if time_ms <= 0.0:
        return 0.0
    time_s = time_ms / 1000.0
    return float(np.exp(-1.0 / (time_s * sample_rate)))


class DynamicsProcessor(gr.sync_block):
    def __init__(self, sample_rate, threshold_db, ratio, knee_db, attack_ms, release_ms,
                 makeup_gain_db=0.0, enabled=True):
        gr.sync_block.__init__(self, name="DynamicsProcessor",
                                in_sig=[np.float32], out_sig=[np.float32])
        self._lock = threading.Lock()
        self._sample_rate = sample_rate
        self._threshold_db = threshold_db
        self._ratio = ratio
        self._knee_db = knee_db
        self._attack_ms = attack_ms
        self._release_ms = release_ms
        self._makeup_gain_db = makeup_gain_db
        self._enabled = enabled
        self._alpha_attack = _alpha_for(attack_ms, sample_rate)
        self._alpha_release = _alpha_for(release_ms, sample_rate)
        self._gain_reduction_db = 0.0  # persists across work() calls -- the smoother's state

    # --- gain computer --------------------------------------------------
    def _static_gain_reduction_db(self, x_db):
        """Vectorized soft-knee gain-reduction curve (always <= 0), evaluated
        pointwise for the whole block -- the only per-sample-dependent part
        left for the caller is the attack/release smoothing recursion."""
        threshold = self._threshold_db
        ratio = self._ratio
        knee = self._knee_db
        below = x_db <= threshold - knee / 2.0
        above = x_db >= threshold + knee / 2.0
        # Soft-knee quadratic blend region:
        knee_term = ((1.0 / ratio - 1.0) * (x_db - threshold + knee / 2.0) ** 2) / (2.0 * max(knee, _EPS))
        y_db = np.where(
            below, x_db,
            np.where(above, threshold + (x_db - threshold) / ratio, x_db + knee_term)
        )
        return y_db - x_db  # gain reduction, <= 0

    def work(self, input_items, output_items):
        in0 = input_items[0]
        out0 = output_items[0]

        with self._lock:
            if not self._enabled:
                out0[:] = in0
                return len(in0)

            alpha_attack = self._alpha_attack
            alpha_release = self._alpha_release
            makeup_lin = 10.0 ** (self._makeup_gain_db / 20.0)
            gr_prev = self._gain_reduction_db

            x_db = 20.0 * np.log10(np.abs(in0) + _EPS)
            gr_target = self._static_gain_reduction_db(x_db)

            # Sequential smoothing recursion -- inherently one-sample-at-a-time
            # (each output depends on the previous smoothed value and a
            # per-sample attack-vs-release branch), kept as tight as possible
            # (2 multiplies, 1 add, 1 compare per sample); everything else in
            # this method is vectorized numpy across the whole block.
            gr_smoothed = np.empty_like(gr_target)
            for i in range(len(gr_target)):
                target = gr_target[i]
                if target < gr_prev:
                    gr_prev = alpha_attack * gr_prev + (1.0 - alpha_attack) * target
                else:
                    gr_prev = alpha_release * gr_prev + (1.0 - alpha_release) * target
                gr_smoothed[i] = gr_prev

            self._gain_reduction_db = float(gr_prev)
            gain_lin = (10.0 ** (gr_smoothed / 20.0)) * makeup_lin
            out0[:] = in0 * gain_lin

        return len(in0)

    # --- runtime control (safe to call anytime, no scheduler/buffer impact
    # -- see module docstring) -------------------------------------------
    def set_threshold_db(self, db):
        with self._lock:
            self._threshold_db = db

    def set_ratio(self, ratio):
        with self._lock:
            self._ratio = max(ratio, 1.0)

    def set_knee_db(self, db):
        with self._lock:
            self._knee_db = max(db, 0.0)

    def set_attack_ms(self, ms):
        with self._lock:
            self._attack_ms = ms
            self._alpha_attack = _alpha_for(ms, self._sample_rate)

    def set_release_ms(self, ms):
        with self._lock:
            self._release_ms = ms
            self._alpha_release = _alpha_for(ms, self._sample_rate)

    def set_makeup_gain_db(self, db):
        with self._lock:
            self._makeup_gain_db = db

    def set_enabled(self, enabled):
        with self._lock:
            self._enabled = enabled
            if enabled:
                # Reset smoother state on re-enable: otherwise it resumes
                # from whatever (possibly deeply negative) reduction was
                # frozen at disable time, producing an audible "duck" right
                # after re-enabling instead of a clean attack-in.
                self._gain_reduction_db = 0.0

    def gain_reduction_db(self):
        """Current smoothed gain reduction in dB (<=0, 0 = no reduction).
        For an optional GUI meter."""
        with self._lock:
            return self._gain_reduction_db
