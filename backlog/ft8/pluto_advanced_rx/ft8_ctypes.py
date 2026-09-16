"""ctypes wrapper around libft8wrap.so (kgoba/ft8_lib, a real, existing
open-source FT8/FT4 encoder+decoder -- reused rather than reimplementing
FT8's LDPC(174,91) forward-error-correction decoder and intricate
message-packing protocol from scratch, an enormous, bug-prone undertaking
that took WSJT-X's own authors years). Built from source by
install-ft8.sh. Mirrors rade_ctypes.py's overall shape (lazy dlopen, an
FT8_AVAILABLE flag, `.restype`/`.argtypes` bound explicitly per function,
a real runtime smoke test before declaring available -- not just symbol
presence).

Unlike RADE (an opaque native session handle via rade_open()/rade_close()),
ft8_lib's structs are FULLY DEFINED, plain C structs with no opaque
pointers at the Python/C boundary except internal buffers ft8_lib itself
allocates and frees (monitor_init()/monitor_free()) -- so this module
mirrors them directly as ctypes.Structure subclasses and lets Python read/
write their fields directly, rather than wrapping every access in a
function call.

Struct field layouts below are transcribed directly from the real, locally
cloned ft8_lib headers at the exact commit install-ft8.sh pins (verified,
not guessed -- see ft8/message.h, ft8/decode.h, common/monitor.h,
fft/kiss_fftr.h in that checkout). If ft8_lib's own struct layout ever
changes upstream, this file's own field order must be updated to match --
same "independent copy that must stay in sync" reasoning already
established in this project for e.g. psk31's own Varicode table copies
(see pluto_advanced_rx/psk31_deframer.py's own docstring).
"""
import ctypes

import numpy as np

FT8_AVAILABLE = False
_lib = None

# --- Protocol constants (ft8/constants.h) -- fixed, not queried at runtime.
FTX_PAYLOAD_LENGTH_BYTES = 10  # holds 77 bits of payload
FT8_NN = 79                    # total channel symbols
FT8_SYMBOL_PERIOD = 0.160      # seconds
FT8_SLOT_TIME = 15.0           # seconds
FT8_TONE_SPACING_HZ = 6.25
FTX_MAX_MESSAGE_FIELDS = 3
FTX_MAX_MESSAGE_LENGTH = 35

FTX_PROTOCOL_FT4 = 0
FTX_PROTOCOL_FT8 = 1

FTX_MESSAGE_RC_OK = 0


# --- Struct mirrors (ft8/message.h) ------------------------------------
class FtxMessage(ctypes.Structure):
    _fields_ = [
        ("payload", ctypes.c_uint8 * FTX_PAYLOAD_LENGTH_BYTES),
        ("hash", ctypes.c_uint16),
    ]


class FtxMessageOffsets(ctypes.Structure):
    _fields_ = [
        ("types", ctypes.c_int * FTX_MAX_MESSAGE_FIELDS),
        ("offsets", ctypes.c_int16 * FTX_MAX_MESSAGE_FIELDS),
    ]


_LOOKUP_HASH_FN = ctypes.CFUNCTYPE(ctypes.c_bool, ctypes.c_int, ctypes.c_uint32, ctypes.c_char_p)
_SAVE_HASH_FN = ctypes.CFUNCTYPE(None, ctypes.c_char_p, ctypes.c_uint32)


class FtxCallsignHashInterface(ctypes.Structure):
    _fields_ = [
        ("lookup_hash", _LOOKUP_HASH_FN),
        ("save_hash", _SAVE_HASH_FN),
    ]


def _noop_lookup_hash(hash_type, h, callsign_out):
    return False


def _noop_save_hash(callsign, n22):
    pass


# --- Struct mirrors (ft8/decode.h) -- WF_ELEM_T is uint8_t in ft8_lib's
# default build (WATERFALL_USE_PHASE is NOT defined -- install-ft8.sh
# builds with plain CFLAGS, no -DWATERFALL_USE_PHASE).
class FtxWaterfall(ctypes.Structure):
    _fields_ = [
        ("max_blocks", ctypes.c_int),
        ("num_blocks", ctypes.c_int),
        ("num_bins", ctypes.c_int),
        ("time_osr", ctypes.c_int),
        ("freq_osr", ctypes.c_int),
        ("mag", ctypes.POINTER(ctypes.c_uint8)),
        ("block_stride", ctypes.c_int),
        ("protocol", ctypes.c_int),
    ]


class FtxCandidate(ctypes.Structure):
    _fields_ = [
        ("score", ctypes.c_int16),
        ("time_offset", ctypes.c_int16),
        ("freq_offset", ctypes.c_int16),
        ("time_sub", ctypes.c_uint8),
        ("freq_sub", ctypes.c_uint8),
    ]


class FtxDecodeStatus(ctypes.Structure):
    _fields_ = [
        ("freq", ctypes.c_float),
        ("time", ctypes.c_float),
        ("ldpc_errors", ctypes.c_int),
        ("crc_extracted", ctypes.c_uint16),
        ("crc_calculated", ctypes.c_uint16),
    ]


# --- Struct mirrors (common/monitor.h) ----------------------------------
class MonitorConfig(ctypes.Structure):
    _fields_ = [
        ("f_min", ctypes.c_float),
        ("f_max", ctypes.c_float),
        ("sample_rate", ctypes.c_int),
        ("time_osr", ctypes.c_int),
        ("freq_osr", ctypes.c_int),
        ("protocol", ctypes.c_int),
    ]


class Monitor(ctypes.Structure):
    """Mirrors monitor_t exactly (non-WATERFALL_USE_PHASE build -- no
    nifft/ifft_work/ifft_cfg fields, those are #ifdef'd out). Every field
    after construction is populated by monitor_init() itself -- this
    Python side only ever reads them (block_size/symbol_period/wf), never
    writes them directly."""
    _fields_ = [
        ("symbol_period", ctypes.c_float),
        ("min_bin", ctypes.c_int),
        ("max_bin", ctypes.c_int),
        ("block_size", ctypes.c_int),
        ("subblock_size", ctypes.c_int),
        ("nfft", ctypes.c_int),
        ("fft_norm", ctypes.c_float),
        ("window", ctypes.POINTER(ctypes.c_float)),
        ("last_frame", ctypes.POINTER(ctypes.c_float)),
        ("wf", FtxWaterfall),
        ("max_mag", ctypes.c_float),
        ("fft_work", ctypes.c_void_p),
        ("fft_cfg", ctypes.c_void_p),  # kiss_fftr_cfg == struct kiss_fftr_state* -- opaque, never dereferenced here
    ]


def _load():
    global _lib, FT8_AVAILABLE

    try:
        lib = ctypes.CDLL("libft8wrap.so")
    except OSError:
        return

    try:
        lib.ftx_message_encode.restype = ctypes.c_int
        lib.ftx_message_encode.argtypes = [
            ctypes.POINTER(FtxMessage), ctypes.POINTER(FtxCallsignHashInterface), ctypes.c_char_p,
        ]
        lib.ftx_message_decode.restype = ctypes.c_int
        lib.ftx_message_decode.argtypes = [
            ctypes.POINTER(FtxMessage), ctypes.POINTER(FtxCallsignHashInterface),
            ctypes.c_char_p, ctypes.POINTER(FtxMessageOffsets),
        ]
        lib.ft8_encode.restype = None
        lib.ft8_encode.argtypes = [ctypes.POINTER(ctypes.c_uint8), ctypes.POINTER(ctypes.c_uint8)]

        lib.monitor_init.restype = None
        lib.monitor_init.argtypes = [ctypes.POINTER(Monitor), ctypes.POINTER(MonitorConfig)]
        lib.monitor_process.restype = None
        lib.monitor_process.argtypes = [ctypes.POINTER(Monitor), ctypes.POINTER(ctypes.c_float)]
        lib.monitor_reset.restype = None
        lib.monitor_reset.argtypes = [ctypes.POINTER(Monitor)]
        lib.monitor_free.restype = None
        lib.monitor_free.argtypes = [ctypes.POINTER(Monitor)]

        lib.ftx_find_candidates.restype = ctypes.c_int
        lib.ftx_find_candidates.argtypes = [
            ctypes.POINTER(FtxWaterfall), ctypes.c_int, ctypes.POINTER(FtxCandidate), ctypes.c_int,
        ]
        lib.ftx_decode_candidate.restype = ctypes.c_bool
        lib.ftx_decode_candidate.argtypes = [
            ctypes.POINTER(FtxWaterfall), ctypes.POINTER(FtxCandidate), ctypes.c_int,
            ctypes.POINTER(FtxMessage), ctypes.POINTER(FtxDecodeStatus),
        ]
    except AttributeError:
        return  # symbol missing -- unexpected build of libft8wrap.so

    # Runtime sanity check, not just symbol presence -- a real pack/tone-
    # encode/unpack round trip, mirroring rade_ctypes.py's own open/close
    # smoke test. Confirmed during this integration's own feasibility
    # spike to reliably distinguish "symbols present" from "ABI actually
    # matches this module's struct layout" (a struct-layout mismatch would
    # typically segfault here, not raise a catchable Python exception --
    # acceptable for an import-time check of a build this same installer
    # just produced).
    try:
        hash_if = FtxCallsignHashInterface(_LOOKUP_HASH_FN(_noop_lookup_hash), _SAVE_HASH_FN(_noop_save_hash))
        msg = FtxMessage()
        # A real, standard-format callsign -- NOT a placeholder like "DX"
        # (found the hard way during this integration's own feasibility
        # spike: ftx_message_encode() packs a non-standard-format token
        # like "DX" via its hashed-callsign path, which this stub hash
        # interface can't resolve back to text on decode, making a
        # correct round trip fail this check for a reason that has
        # nothing to do with whether the library/bindings actually work).
        rc = lib.ftx_message_encode(ctypes.byref(msg), ctypes.byref(hash_if), b"CQ DA2JH JO31")
        if rc != FTX_MESSAGE_RC_OK:
            return
        tones = (ctypes.c_uint8 * FT8_NN)()
        lib.ft8_encode(msg.payload, tones)
        out = ctypes.create_string_buffer(FTX_MAX_MESSAGE_LENGTH + 1)
        offsets = FtxMessageOffsets()
        rc = lib.ftx_message_decode(ctypes.byref(msg), ctypes.byref(hash_if), out, ctypes.byref(offsets))
        if rc != FTX_MESSAGE_RC_OK or out.value != b"CQ DA2JH JO31":
            return
    except Exception:
        return

    _lib = lib
    FT8_AVAILABLE = True


_load()


def encode_message(text: str):
    """Pack a plain-text FT8 message and generate its 79-tone sequence
    (values 0..7). Returns a uint8 numpy array of length FT8_NN, or None
    if the message can't be packed (e.g. malformed callsign/grid --
    ftx_message_encode() itself does the message-type detection, no
    protocol logic reimplemented here at all)."""
    if not FT8_AVAILABLE:
        raise RuntimeError("FT8 support not available (see install-ft8.sh)")
    hash_if = FtxCallsignHashInterface(_LOOKUP_HASH_FN(_noop_lookup_hash), _SAVE_HASH_FN(_noop_save_hash))
    msg = FtxMessage()
    rc = _lib.ftx_message_encode(ctypes.byref(msg), ctypes.byref(hash_if), text.encode("ascii", "replace"))
    if rc != FTX_MESSAGE_RC_OK:
        return None
    tones = (ctypes.c_uint8 * FT8_NN)()
    _lib.ft8_encode(msg.payload, tones)
    return np.frombuffer(bytes(tones), dtype=np.uint8).copy()


class Ft8Monitor:
    """Wraps one monitor_t (audio -> waterfall accumulator) + the find/
    decode step, for ONE 15-second FT8 slot at a time. Not thread-safe --
    use from a single thread, same assumption as RadeSession."""

    def __init__(self, sample_rate: int, f_min=200.0, f_max=3000.0, time_osr=2, freq_osr=2):
        if not FT8_AVAILABLE:
            raise RuntimeError("FT8 support not available (see install-ft8.sh)")
        self._m = Monitor()
        cfg = MonitorConfig(f_min, f_max, sample_rate, time_osr, freq_osr, FTX_PROTOCOL_FT8)
        _lib.monitor_init(ctypes.byref(self._m), ctypes.byref(cfg))
        self.block_size = self._m.block_size      # samples per monitor_process() call
        self.symbol_period = self._m.symbol_period  # seconds

    def process(self, frame: np.ndarray):
        """frame: float32 array of EXACTLY self.block_size samples (one
        FT8 symbol period's worth) -- call once per symbol period while
        a slot is being captured."""
        frame = np.ascontiguousarray(frame, dtype=np.float32)
        if frame.size != self.block_size:
            raise ValueError(f"expected {self.block_size} samples, got {frame.size}")
        _lib.monitor_process(ctypes.byref(self._m), frame.ctypes.data_as(ctypes.POINTER(ctypes.c_float)))

    def reset(self):
        """Call once a slot's candidates have been searched/decoded, before
        starting to accumulate the next slot."""
        _lib.monitor_reset(ctypes.byref(self._m))

    @property
    def num_blocks(self) -> int:
        return self._m.wf.num_blocks

    def find_and_decode(self, num_candidates=32, min_score=10, max_iterations=25):
        """Runs ftx_find_candidates() + ftx_decode_candidate() +
        ftx_message_decode() over whatever's been accumulated so far.
        Returns a list of (text: str, status: FtxDecodeStatus) for every
        candidate that decoded successfully (CRC-checked internally by
        ftx_decode_candidate() -- only real passes are returned)."""
        heap = (FtxCandidate * num_candidates)()
        n = _lib.ftx_find_candidates(ctypes.byref(self._m.wf), num_candidates, heap, min_score)
        hash_if = FtxCallsignHashInterface(_LOOKUP_HASH_FN(_noop_lookup_hash), _SAVE_HASH_FN(_noop_save_hash))
        results = []
        for i in range(n):
            msg = FtxMessage()
            status = FtxDecodeStatus()
            ok = _lib.ftx_decode_candidate(ctypes.byref(self._m.wf), ctypes.byref(heap[i]), max_iterations,
                                            ctypes.byref(msg), ctypes.byref(status))
            if not ok:
                continue
            out = ctypes.create_string_buffer(FTX_MAX_MESSAGE_LENGTH + 1)
            offsets = FtxMessageOffsets()
            rc = _lib.ftx_message_decode(ctypes.byref(msg), ctypes.byref(hash_if), out, ctypes.byref(offsets))
            if rc != FTX_MESSAGE_RC_OK:
                continue
            results.append((out.value.decode("ascii", "replace"), status))
        return results

    def close(self):
        if self._m is not None:
            _lib.monitor_free(ctypes.byref(self._m))
            self._m = None

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass
