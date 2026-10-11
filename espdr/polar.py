"""Complex baseband (IQ) to the records of the polar transmitter.

The chip has no I/Q input, but it can move its carrier in frequency 40000 times a second (see `txmodes`). A signal with a constant
envelope (FM, M17, any FSK) is fully described by its instantaneous frequency, so it can be played by taking the frequency out of the
IQ samples and sending it as the frequency field of the records, with a constant gain code. The envelope is ignored.

`IqToPolar` takes the frequency from the phase difference of consecutive samples: `f = arg(x[n] * conj(x[n-1])) * fs / 2 pi`. A
sample whose magnitude is below `threshold` (silence, or the zeros a muted flowgraph delivers) carries no phase: the last frequency
is held. The state survives block boundaries, so blocks of any size give the same result.
"""
import numpy as np

from . import dsp, txmodes


class IqToPolar:
    """The frequency in Hz of a complex baseband signal, one value per update (default 40000 a second), for blocks of any size."""

    def __init__(self, rate_in, update_rate=40000, threshold=1e-3):
        self.rate_in, self.update_rate, self.threshold = float(rate_in), int(update_rate), float(threshold)
        if self.rate_in < self.update_rate:
            raise ValueError("the IQ rate must be at least the update rate")
        ratio = self.rate_in / self.update_rate
        # a whole ratio (80 kHz -> 40 kHz) averages groups of samples; any other ratio resamples the frequency track
        self.group = int(round(ratio)) if abs(ratio - round(ratio)) < 1e-9 else None
        self.res = None if self.group else dsp.Resampler(self.rate_in, self.update_rate)
        self.prev = 0j          # the last sample of the previous block
        self.f_last = 0.0       # the last valid frequency, held through silence
        self.rest = np.zeros(0) # frequencies that do not fill a group yet

    def reset(self):
        self.prev, self.f_last, self.rest = 0j, 0.0, np.zeros(0)
        if self.res is not None:
            self.res = dsp.Resampler(self.rate_in, self.update_rate)

    def per_sample(self, x):
        """The frequency at every input sample in Hz, held through samples too weak to carry a phase."""
        x = np.asarray(x, dtype=np.complex128)
        if not len(x):
            return np.zeros(0)
        prev = np.concatenate([[self.prev], x[:-1]])
        self.prev = x[-1]
        valid = (np.abs(x) > self.threshold) & (np.abs(prev) > self.threshold)
        f = np.angle(x * np.conj(prev)) * (self.rate_in / (2 * np.pi))
        idx = np.where(valid, np.arange(len(x)), -1)
        np.maximum.accumulate(idx, out=idx)
        held = np.concatenate([[self.f_last], f])[idx + 1]       # idx -1: nothing valid in this block yet, keep the old value
        self.f_last = float(held[-1])
        return held

    def process(self, x):
        f = self.per_sample(x)
        if self.group is None:
            return self.res.process(f)
        data = np.concatenate([self.rest, f])
        m = len(data) // self.group
        self.rest = data[m * self.group:]
        return data[:m * self.group].reshape(m, self.group).mean(axis=1)


class IqModulator:
    """IQ blocks to records, like the other modulators of `txmodes`: `process(x)` returns uint32 records. `static_hz` is a constant
    added to every record (the signal's centre against the LO, see `txlink.plan_lo`); `clipped` counts records the firmware's range
    (+-19 kHz around the LO) forced the frequency of."""

    name = "IQ"

    def __init__(self, rate_in, rate=40000, power_db=0.0, static_hz=0.0, threshold=1e-3):
        self.rate = rate
        self.polar = IqToPolar(rate_in, rate, threshold)
        self.fsk = txmodes.FskModulator(rate=rate, power_db=power_db, static_hz=static_hz)
        self.clipped = 0

    @property
    def static_hz(self):
        return self.fsk.static_hz

    def set_power(self, power_db):
        """Changes the gain code of the records that follow."""
        self.fsk.code = txmodes.peak_code(power_db)

    def process(self, x):
        f = self.polar.process(x)
        if not len(f):
            return np.zeros(0, dtype=np.uint32)
        q4 = np.rint(f / txmodes.Q4_HZ) + self.fsk.static_q4
        self.clipped += int((np.abs(q4) > txmodes.MAX_Q4).sum())
        return self.fsk.process(f)
