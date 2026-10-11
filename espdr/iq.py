"""Complex baseband input for `espdr-tx -m iq`: files and pipes of interleaved I/Q samples, in blocks, any size of read."""
import os
import sys

import numpy as np

# format: (dtype of one value, scale to +-1). cf32 is GNU Radio's complex64 and espdr-rx's `--convert cf32`; cs8 and cs16 are the
# receiver's raw formats.
FORMATS = {"cf32": (np.dtype("<f4"), 1.0), "cs16": (np.dtype("<i2"), 1.0 / 32768.0), "cs8": (np.dtype("i1"), 1.0 / 128.0)}


class IqError(Exception):
    pass


class IqSource:
    """Blocks of complex samples (complex128, full scale 1) from a binary file object. `fraction()` is how much of a file has been read
    (0 for a pipe)."""

    def __init__(self, fh, rate, fmt="cf32", block_s=0.02, name="stdin", size=None):
        if fmt not in FORMATS:
            raise IqError(f"unknown IQ format {fmt!r}; use one of {', '.join(sorted(FORMATS))}")
        self.fh, self.rate, self.fmt, self.name, self.size = fh, float(rate), fmt, name, size
        self.dtype, self.scale = FORMATS[fmt]
        self.block_bytes = max(1, int(self.rate * block_s)) * 2 * self.dtype.itemsize
        self.pos = 0

    def fraction(self):
        return min(1.0, self.pos / self.size) if self.size else 0.0

    def blocks(self):
        pair = 2 * self.dtype.itemsize
        carry = b""
        while True:
            data = self.fh.read(self.block_bytes - len(carry))
            if not data:
                break                                   # the end; a half sample at the end is dropped
            self.pos += len(data)
            data = carry + data
            n = len(data) // pair * pair
            carry = data[n:]
            if not n:
                continue
            v = np.frombuffer(data[:n], dtype=self.dtype).astype(np.float64) * self.scale
            yield v[0::2] + 1j * v[1::2]

    def close(self):
        if self.fh is not sys.stdin.buffer:
            self.fh.close()


def open_iq(spec, rate, fmt="cf32"):
    """`spec` is a file name, or '-' for standard input."""
    if spec == "-":
        return IqSource(sys.stdin.buffer, rate, fmt, name="stdin")
    try:
        fh = open(spec, "rb")
    except OSError as e:
        raise IqError(f"cannot open {spec}: {e.strerror or e}") from None
    return IqSource(fh, rate, fmt, name=os.path.basename(spec), size=os.fstat(fh.fileno()).st_size)
