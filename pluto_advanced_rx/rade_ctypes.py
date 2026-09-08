"""ctypes wrapper around librade.so's rade_api.h (RADE V1 modem: features <->
IQ). Built from source by install-rade.sh (freedv/rade_c) -- no upstream
Python bindings exist. Mirrors freedv_ctypes.py's overall shape (lazy
dlopen, an *_AVAILABLE flag, a Session class wrapping one native handle).

RADE_COMP (rade_api.h: struct { float real; float imag; }) is binary-
identical to numpy's complex64 (two contiguous float32, no padding -- both
fields are 4-byte and the struct is naturally 8-byte aligned), so IQ
buffers are passed as plain numpy complex64 arrays reinterpreted through a
ctypes.Structure pointer, no manual (re,im) unpacking needed anywhere.

V1 only (flags=0, no RADE_MODE_V2) -- confirmed via radae_tx.c/radae_rx.c's
own CLI tools, which pass flags=0 by default; RADE V2 is explicitly marked
"not recommended for on-air use" in rade_c's own README. model_file is a
placeholder argument to rade_open() -- confirmed ignored at runtime (built-
in weights), same as the CLI tools' own default value.
"""
import ctypes
import shutil

import numpy as np

RADE_AVAILABLE = False
_lib = None


class RadeComp(ctypes.Structure):
    _fields_ = [("real", ctypes.c_float), ("imag", ctypes.c_float)]


def _load():
    global _lib, RADE_AVAILABLE

    # Both artifacts are required -- librade.so alone can't synthesize
    # speech (the FARGAN/LPCNet speech<->feature conversion is NOT exported
    # by librade.so, confirmed via nm -D during this integration's
    # feasibility spike; lpcnet_demo, a separate CLI tool built alongside
    # it, is the only way to reach it -- see lpcnet_subprocess.py).
    if shutil.which("lpcnet_demo") is None:
        return

    try:
        lib = ctypes.CDLL("librade.so")
    except OSError:
        return

    try:
        lib.rade_initialize.restype = None
        lib.rade_initialize.argtypes = []
        lib.rade_open.restype = ctypes.c_void_p
        lib.rade_open.argtypes = [ctypes.c_char_p, ctypes.c_int]
        lib.rade_close.restype = None
        lib.rade_close.argtypes = [ctypes.c_void_p]
        lib.rade_n_tx_out.restype = ctypes.c_int
        lib.rade_n_tx_out.argtypes = [ctypes.c_void_p]
        lib.rade_n_tx_eoo_out.restype = ctypes.c_int
        lib.rade_n_tx_eoo_out.argtypes = [ctypes.c_void_p]
        lib.rade_nin_max.restype = ctypes.c_int
        lib.rade_nin_max.argtypes = [ctypes.c_void_p]
        lib.rade_n_features_in_out.restype = ctypes.c_int
        lib.rade_n_features_in_out.argtypes = [ctypes.c_void_p]
        lib.rade_n_eoo_bits.restype = ctypes.c_int
        lib.rade_n_eoo_bits.argtypes = [ctypes.c_void_p]
        lib.rade_tx.restype = ctypes.c_int
        lib.rade_tx.argtypes = [ctypes.c_void_p, ctypes.POINTER(RadeComp), ctypes.POINTER(ctypes.c_float)]
        lib.rade_tx_set_eoo_bits.restype = None
        lib.rade_tx_set_eoo_bits.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_float)]
        lib.rade_tx_eoo.restype = ctypes.c_int
        lib.rade_tx_eoo.argtypes = [ctypes.c_void_p, ctypes.POINTER(RadeComp)]
        lib.rade_nin.restype = ctypes.c_int
        lib.rade_nin.argtypes = [ctypes.c_void_p]
        lib.rade_rx.restype = ctypes.c_int
        lib.rade_rx.argtypes = [
            ctypes.c_void_p, ctypes.POINTER(ctypes.c_float), ctypes.POINTER(ctypes.c_int),
            ctypes.POINTER(ctypes.c_float), ctypes.POINTER(RadeComp),
        ]
        lib.rade_sync.restype = ctypes.c_int
        lib.rade_sync.argtypes = [ctypes.c_void_p]
        lib.rade_freq_offset.restype = ctypes.c_float
        lib.rade_freq_offset.argtypes = [ctypes.c_void_p]
        lib.rade_snrdB_3k_est.restype = ctypes.c_float
        lib.rade_snrdB_3k_est.argtypes = [ctypes.c_void_p]
    except AttributeError:
        return  # symbol missing -- unexpected build of librade.so

    lib.rade_initialize()  # must run before any other RADE function (rade_api.h)

    # Runtime sanity check, not just symbol presence -- open+close a real
    # V1 session once at import time, mirroring freedv_ctypes.py.
    handle = lib.rade_open(b"", 0)
    if not handle:
        return
    lib.rade_close(handle)

    _lib = lib
    RADE_AVAILABLE = True


_load()


class RadeSession:
    """One rade_open() handle (V1). Not thread-safe -- use from a single
    thread (satisfied naturally when driven from one GNU Radio block's work
    thread, same assumption gr-m17's blocks and FreeDVSession make)."""

    def __init__(self):
        if not RADE_AVAILABLE:
            raise RuntimeError("RADE V1 support not available (see install-rade.sh)")
        self._r = _lib.rade_open(b"", 0)  # V1: flags=0, model_file ignored (built-in weights)
        if not self._r:
            raise RuntimeError("rade_open() failed")

        self.n_features_in_out = _lib.rade_n_features_in_out(self._r)
        self.n_tx_out = _lib.rade_n_tx_out(self._r)
        self.n_tx_eoo_out = _lib.rade_n_tx_eoo_out(self._r)
        self.n_eoo_bits = _lib.rade_n_eoo_bits(self._r)
        self.nin_max = _lib.rade_nin_max(self._r)

    def nin(self) -> int:
        """RX input frame size is NOT fixed -- must be queried before EVERY
        rx() call (rade_api.h's own comment on rade_nin())."""
        return _lib.rade_nin(self._r)

    def tx(self, features_in: np.ndarray) -> np.ndarray:
        """features_in: float32 array of exactly n_features_in_out. Returns
        a complex64 array of exactly n_tx_out IQ samples @ 8kHz."""
        features_in = np.ascontiguousarray(features_in, dtype=np.float32)
        if features_in.size != self.n_features_in_out:
            raise ValueError(f"expected {self.n_features_in_out} features, got {features_in.size}")
        tx_out = np.empty(self.n_tx_out, dtype=np.complex64)
        n = _lib.rade_tx(
            self._r, tx_out.ctypes.data_as(ctypes.POINTER(RadeComp)),
            features_in.ctypes.data_as(ctypes.POINTER(ctypes.c_float)),
        )
        return tx_out[:n]

    def tx_eoo(self) -> np.ndarray:
        """One-shot End-of-Over IQ tail. Returns a complex64 array of
        exactly n_tx_eoo_out samples."""
        eoo_out = np.empty(self.n_tx_eoo_out, dtype=np.complex64)
        n = _lib.rade_tx_eoo(self._r, eoo_out.ctypes.data_as(ctypes.POINTER(RadeComp)))
        return eoo_out[:n]

    def rx(self, rx_in: np.ndarray):
        """rx_in: complex64 array of exactly nin() samples (call nin()
        first -- the required size changes call to call). Returns
        (features_out_or_None, has_eoo: bool, eoo_bits_or_None)."""
        rx_in = np.ascontiguousarray(rx_in, dtype=np.complex64)
        features_out = np.empty(self.n_features_in_out, dtype=np.float32)
        has_eoo = ctypes.c_int(0)
        eoo_out = np.empty(max(self.n_eoo_bits, 1), dtype=np.float32)
        n = _lib.rade_rx(
            self._r,
            features_out.ctypes.data_as(ctypes.POINTER(ctypes.c_float)),
            ctypes.byref(has_eoo),
            eoo_out.ctypes.data_as(ctypes.POINTER(ctypes.c_float)),
            rx_in.ctypes.data_as(ctypes.POINTER(RadeComp)),
        )
        if n <= 0:
            return None, bool(has_eoo.value), None
        eoo_bits = eoo_out[:self.n_eoo_bits].copy() if has_eoo.value else None
        return features_out[:n].copy(), bool(has_eoo.value), eoo_bits

    def sync(self) -> bool:
        return bool(_lib.rade_sync(self._r))

    def freq_offset(self) -> float:
        return float(_lib.rade_freq_offset(self._r))

    def snr_db(self) -> float:
        return float(_lib.rade_snrdB_3k_est(self._r))

    def close(self):
        if self._r is not None:
            _lib.rade_close(self._r)
            self._r = None

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass
