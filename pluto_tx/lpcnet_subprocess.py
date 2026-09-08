"""Persistent subprocess wrapper around lpcnet_demo (built by
install-rade.sh, part of freedv/rade_c's own build) for the two streaming
modes used here. No Python bindings exist for this vocoder -- it's a
standalone CLI tool, not something librade.so exports (confirmed via nm -D:
the FARGAN/LPCNet speech<->feature-vector functions are entirely absent
from librade.so). rade_c's own RadeAPIUse.md confirms lpcnet_demo's
`-features`/`-fargan-synthesis` modes -- reading/writing '-' for stdin/
stdout -- are the officially intended integration path, not a workaround.

Two modes, with DIFFERENT framing (confirmed by reading lpcnet_demo.c's own
source, not just observing CLI behavior -- this mattered, see below):

  MODE_FEATURES ("-features"): speech -> features. Pure 1:1 streaming --
  feed LPCNET_FRAME_SIZE (160) int16 PCM samples, read back
  NB_TOTAL_FEATURES (36) float32 features, from the very first frame.

  MODE_FARGAN_SYNTHESIS ("-fargan-synthesis"): features -> speech. NOT pure
  1:1 -- lpcnet_demo.c's own main loop reads PRIMING_FRAMES (5) feature
  frames upfront and feeds them to fargan_cont() as FARGAN's warm-up/
  continuity context, producing NO PCM output for them at all; only the
  6th frame onward is a clean 1:1 filter (1 features frame in -> 1 PCM
  frame of LPCNET_FRAME_SIZE samples out). Callers must feed 5 "priming"
  frames before the first real PCM frame arrives -- synthesize_frame()
  returns None for those, see its docstring. This resolves what the RADE
  integration plan flagged as an open question needing empirical
  observation -- turned out to be a fixed, deterministic priming count
  visible directly in the source, not something that varies at runtime.
"""
import shutil
import subprocess

import numpy as np

# lpcnet_demo.c writes every frame via plain fwrite(), with no fflush()/
# setvbuf() anywhere in its source (confirmed by reading it) -- glibc's
# stdio defaults to fully-buffered (several KB) when stdout isn't a TTY, so
# without this, our synchronous "write one frame, immediately read the
# matching output frame" protocol below deadlocks: the child has the bytes
# sitting in its own libc buffer, not yet written to the pipe, and our read
# blocks waiting for them forever. `stdbuf -o0 -i0` (coreutils, LD_PRELOADs
# libstdbuf.so to force unbuffered stdio in the child) fixes this with no
# need to patch/rebuild the vendored source. Confirmed this session: the
# exact same round-trip hangs without it, and completes immediately with it.
# coreutils (and therefore stdbuf) is a base package on every Debian/Ubuntu
# install this project targets -- treated as a hard requirement, not an
# optional degrade, since silently running without it would reintroduce the
# deadlock rather than just losing a nice-to-have.
_STDBUF_PREFIX = ["stdbuf", "-o0", "-i0"]

LPCNET_FRAME_SIZE = 160    # PCM samples per frame (10ms @ 16kHz)
NB_TOTAL_FEATURES = 36     # floats per feature frame

MODE_FEATURES = "features"
MODE_FARGAN_SYNTHESIS = "fargan-synthesis"

# MODE_FARGAN_SYNTHESIS only -- see module docstring.
PRIMING_FRAMES = 5


def lpcnet_demo_available() -> bool:
    return shutil.which("lpcnet_demo") is not None


class LpcnetDemoProcess:
    """One persistent `lpcnet_demo -<mode> - -` subprocess, stdin/stdout as
    raw binary pipes (bufsize=0 -- no Python-level buffering, frame-exact
    writes/reads only). Not thread-safe -- use from a single thread, same
    assumption as RadeSession (both are driven from one GNU Radio block's
    work thread). Each write is small (144 or 320 bytes, both well under a
    pipe's OS buffer size) and immediately followed by a blocking read of
    the matching response size, so there's no risk of the classic
    subprocess.PIPE bidirectional deadlock (child stdout filling up while
    we're still writing) at these frame sizes."""

    def __init__(self, mode: str):
        if mode not in (MODE_FEATURES, MODE_FARGAN_SYNTHESIS):
            raise ValueError(f"unknown mode {mode!r}")
        exe = shutil.which("lpcnet_demo")
        if exe is None:
            raise RuntimeError("lpcnet_demo not found on PATH -- run install-rade.sh")
        if shutil.which("stdbuf") is None:
            raise RuntimeError("stdbuf (coreutils) not found -- required to avoid a stdio-buffering "
                                "deadlock with lpcnet_demo, see this module's docstring")
        self.mode = mode
        self._frames_fed = 0
        self._proc = subprocess.Popen(
            [*_STDBUF_PREFIX, exe, f"-{mode}", "-", "-"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            bufsize=0,
        )

    def encode_frame(self, pcm: np.ndarray) -> np.ndarray:
        """MODE_FEATURES only. pcm: int16 array of exactly
        LPCNET_FRAME_SIZE. Returns a float32 array of exactly
        NB_TOTAL_FEATURES."""
        assert self.mode == MODE_FEATURES
        pcm = np.ascontiguousarray(pcm, dtype=np.int16)
        if pcm.size != LPCNET_FRAME_SIZE:
            raise ValueError(f"expected {LPCNET_FRAME_SIZE} PCM samples, got {pcm.size}")
        self._proc.stdin.write(pcm.tobytes())
        self._proc.stdin.flush()
        raw = self._read_exact(NB_TOTAL_FEATURES * 4)
        return np.frombuffer(raw, dtype=np.float32).copy()

    def synthesize_frame(self, features: np.ndarray):
        """MODE_FARGAN_SYNTHESIS only. features: float32 array of exactly
        NB_TOTAL_FEATURES. Returns an int16 array of exactly
        LPCNET_FRAME_SIZE PCM samples -- but see the module docstring: the
        first PRIMING_FRAMES calls return None (consumed as FARGAN warm-up,
        no PCM produced yet); from the (PRIMING_FRAMES+1)-th call onward
        each call returns exactly one PCM frame."""
        assert self.mode == MODE_FARGAN_SYNTHESIS
        features = np.ascontiguousarray(features, dtype=np.float32)
        if features.size != NB_TOTAL_FEATURES:
            raise ValueError(f"expected {NB_TOTAL_FEATURES} features, got {features.size}")
        self._proc.stdin.write(features.tobytes())
        self._proc.stdin.flush()
        self._frames_fed += 1
        if self._frames_fed <= PRIMING_FRAMES:
            return None
        raw = self._read_exact(LPCNET_FRAME_SIZE * 2)
        return np.frombuffer(raw, dtype=np.int16).copy()

    def _read_exact(self, n_bytes: int) -> bytes:
        buf = bytearray()
        while len(buf) < n_bytes:
            chunk = self._proc.stdout.read(n_bytes - len(buf))
            if not chunk:
                raise RuntimeError("lpcnet_demo exited unexpectedly")
            buf.extend(chunk)
        return bytes(buf)

    def close(self):
        if self._proc is None:
            return
        if self._proc.poll() is None:
            try:
                self._proc.stdin.close()
            except Exception:
                pass
            self._proc.terminate()
            try:
                self._proc.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                self._proc.kill()
        self._proc = None

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass
