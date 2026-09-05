"""ctypes wrapper around libcodec2's freedv_api (FreeDV 2020/2020B TX) plus
the reliable_text API used for the station-ID text sideband.

No Python bindings exist upstream for freedv_api (only for the raw Codec2
vocoder, e.g. pycodec2) -- this hand-writes the small subset of the C API
needed for TX, following the same ctypes approach codec2's own demo/*.py
scripts use. Signatures verified against codec2's src/freedv_api.h and
src/reliable_text.h (github.com/drowe67/codec2).

Unlike gr-m17, this needs NO separate install script and NO from-source
build: libcodec2 (with full LPCNet/2020/2020B support) is already a
transitive dependency of this app's existing `gnuradio` apt package, via
libgnuradio-vocoder -- confirmed on a real system this session (dpkg -l,
apt-cache rdepends, and a direct runtime freedv_open()/freedv_tx() smoke
test all succeeded with zero extra packages installed). FREEDV_AVAILABLE
below is therefore a defensive check (older libcodec2 without 2020/2020B,
or the library missing entirely), not an expected-to-usually-fail path.
"""
import ctypes
import ctypes.util

import numpy as np

FREEDV_MODE_2020 = 8
FREEDV_MODE_2020B = 16

FREEDV_AVAILABLE = False
_lib = None


def _load():
    global _lib, FREEDV_AVAILABLE

    lib = None
    for name in ("libcodec2.so.1.2", "libcodec2.so.1", "libcodec2.so"):
        try:
            lib = ctypes.CDLL(name)
            break
        except OSError:
            continue
    if lib is None:
        path = ctypes.util.find_library("codec2")
        if path:
            try:
                lib = ctypes.CDLL(path)
            except OSError:
                lib = None
    if lib is None:
        return

    try:
        lib.freedv_open.restype = ctypes.c_void_p
        lib.freedv_open.argtypes = [ctypes.c_int]
        lib.freedv_close.restype = None
        lib.freedv_close.argtypes = [ctypes.c_void_p]
        lib.freedv_tx.restype = None
        lib.freedv_tx.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_short),
            ctypes.POINTER(ctypes.c_short),
        ]
        lib.freedv_get_n_speech_samples.restype = ctypes.c_int
        lib.freedv_get_n_speech_samples.argtypes = [ctypes.c_void_p]
        lib.freedv_get_n_nom_modem_samples.restype = ctypes.c_int
        lib.freedv_get_n_nom_modem_samples.argtypes = [ctypes.c_void_p]
        lib.freedv_get_speech_sample_rate.restype = ctypes.c_int
        lib.freedv_get_speech_sample_rate.argtypes = [ctypes.c_void_p]
        lib.freedv_get_modem_sample_rate.restype = ctypes.c_int
        lib.freedv_get_modem_sample_rate.argtypes = [ctypes.c_void_p]

        lib.reliable_text_create.restype = ctypes.c_void_p
        lib.reliable_text_create.argtypes = []
        lib.reliable_text_destroy.restype = None
        lib.reliable_text_destroy.argtypes = [ctypes.c_void_p]
        lib.reliable_text_set_string.restype = None
        lib.reliable_text_set_string.argtypes = [
            ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int,
        ]
        # on_text_rx_t callback + state: NULL/NULL is fine for TX-only use
        # (that callback only fires on the RX side).
        lib.reliable_text_use_with_freedv.restype = None
        lib.reliable_text_use_with_freedv.argtypes = [
            ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
        ]
        lib.reliable_text_unlink_from_freedv.restype = None
        lib.reliable_text_unlink_from_freedv.argtypes = [ctypes.c_void_p]
    except AttributeError:
        # Symbol missing -- too old a libcodec2 build.
        return

    # Runtime sanity check, not just symbol presence: some distro builds of
    # libcodec2 omit LPCNet (2020/2020B need it) even though the base
    # freedv_api symbols still exist. Open+close both modes once at import
    # time so FREEDV_AVAILABLE reflects what actually works here.
    for mode in (FREEDV_MODE_2020, FREEDV_MODE_2020B):
        handle = lib.freedv_open(mode)
        if not handle:
            return
        lib.freedv_close(handle)

    _lib = lib
    FREEDV_AVAILABLE = True


_load()


class FreeDVSession:
    """One freedv_open() handle. Not thread-safe -- use from a single
    thread (satisfied naturally when driven from one GNU Radio block's
    work thread, same assumption gr-m17's blocks make about their own
    state).

    reliable_text (the station-ID text sideband) is DELIBERATELY NOT
    wired up here, despite the ctypes signatures still being declared
    above -- linking a reliable_text object via reliable_text_use_with_
    freedv() corrupts this specific Ubuntu-packaged libcodec2 (1.2.0-4)'s
    FreeDV 2020/2020B TX state, causing a deterministic SIGFPE (integer
    divide-by-zero) deep inside freedv_comptx_2020() on the very next
    freedv_tx() call. Confirmed via gdb: the crash happens even with a
    real (non-NULL) callback and even when set_string() is never called
    -- linking alone is enough. Confirmed NOT a bug in this wrapper: the
    exact same crash reproduces with raw ad-hoc ctypes calls completely
    independent of this class, and disappears entirely when reliable_text
    is never touched at all (freedv_open -> freedv_tx alone works
    reliably, every time). This is a real bug in the packaged library,
    not something fixable from Python -- see the FreeDV section of
    README.md and its ToDo entry for the full writeup. set_text() below
    is a no-op until this is resolved (e.g. a newer libcodec2, or
    reporting upstream)."""

    def __init__(self, mode: int):
        if not FREEDV_AVAILABLE:
            raise RuntimeError("libcodec2 FreeDV 2020/2020B support not available")
        self._freedv = _lib.freedv_open(mode)
        if not self._freedv:
            raise RuntimeError(f"freedv_open({mode}) failed")
        self.mode = mode

        self.n_speech_samples = _lib.freedv_get_n_speech_samples(self._freedv)
        self.n_nom_modem_samples = _lib.freedv_get_n_nom_modem_samples(self._freedv)
        self.speech_sample_rate = _lib.freedv_get_speech_sample_rate(self._freedv)
        self.modem_sample_rate = _lib.freedv_get_modem_sample_rate(self._freedv)

    def set_text(self, text: str):
        """No-op -- see the FreeDVSession docstring above: reliable_text
        integration is disabled due to a confirmed upstream libcodec2 crash."""
        pass

    def tx(self, speech_in: np.ndarray) -> np.ndarray:
        """speech_in: int16 numpy array of exactly n_speech_samples.
        Returns an int16 numpy array of exactly n_nom_modem_samples."""
        speech_in = np.ascontiguousarray(speech_in, dtype=np.int16)
        if speech_in.size != self.n_speech_samples:
            raise ValueError(
                f"expected {self.n_speech_samples} speech samples, got {speech_in.size}"
            )
        mod_out = np.empty(self.n_nom_modem_samples, dtype=np.int16)
        _lib.freedv_tx(
            self._freedv,
            mod_out.ctypes.data_as(ctypes.POINTER(ctypes.c_short)),
            speech_in.ctypes.data_as(ctypes.POINTER(ctypes.c_short)),
        )
        return mod_out

    def close(self):
        if self._freedv is not None:
            _lib.freedv_close(self._freedv)
            self._freedv = None

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass
