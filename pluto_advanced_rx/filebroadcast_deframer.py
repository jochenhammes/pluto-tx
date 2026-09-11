"""GNU Radio sink block that deframes File Broadcast frames straight out of
digital.gfsk_demod's raw bit stream (1 bit per byte, unpacked, values 0/1)
and invokes a plain Python callback per successfully-CRC-checked frame --
same "gr.sync_block with out_sig=None, Python callback/poll surface" idiom
already proven by fft_probe.py's FftProbe, just event-driven (a callback per
frame) instead of poll-driven (a latest-row getter), since a frame is a
discrete event rather than a continuously-updating value.

Frame-parsing helpers (CRC-16, header-length/total-length-from-header) are a
SEPARATE, independent copy of pluto_tx/filebroadcast.py's encoder-side
logic, not an import of it -- matches this project's established convention
for protocol/PHY logic that must stay byte-for-byte in sync across the two
independent app packages (see pluto_advanced_rx/config.py's own module
docstring, and e.g. rade_ctypes.py's per-package copies). If you change the
frame format, change BOTH copies.

digital.gfsk_demod's raw bit output has no inherent byte alignment -- this
block finds frame starts via a bit-by-bit sliding-window search for
SYNC_WORD, then collects bytes (repacked np.packbits(..., bitorder="big"),
matching gfsk_mod(do_unpack=True)'s MSB-first convention -- confirmed
empirically during Phase 0's real-hardware PHY testing) until enough of the
type-specific header is known to determine the frame's total length, then
verifies its CRC-16 before calling on_frame(). Any failure along the way
(unrecognized type, a DATA frame's length field exceeding max_chunk_size, a
bad CRC) silently abandons the in-progress frame and resumes searching for
the next SYNC_WORD -- normal, expected behavior on a lossy real RF link,
not an error worth logging on every occurrence."""
import struct
import threading

import numpy as np
from gnuradio import gr

SYNC_WORD = bytes.fromhex("1ACFFC1D")  # MUST match pluto_tx/filebroadcast.py's SYNC_WORD exactly

FRAME_TYPE_DIRECTORY = 0x01
FRAME_TYPE_DATA = 0x02

_SYNC_BITS = np.unpackbits(np.frombuffer(SYNC_WORD, dtype=np.uint8), bitorder="big")


def _crc16(data: bytes) -> int:
    """MUST match pluto_tx/filebroadcast.py's crc16() exactly."""
    crc = 0xFFFF
    for b in data:
        crc ^= b << 8
        for _ in range(8):
            if crc & 0x8000:
                crc = ((crc << 1) ^ 0x1021) & 0xFFFF
            else:
                crc = (crc << 1) & 0xFFFF
    return crc


def _header_len_for_type(type_byte):
    if type_byte == FRAME_TYPE_DIRECTORY:
        return 3
    if type_byte == FRAME_TYPE_DATA:
        return 8
    return None


def _total_frame_len(header_bytes, max_chunk_size):
    type_byte = header_bytes[0]
    if type_byte == FRAME_TYPE_DIRECTORY:
        name_len = header_bytes[2]
        return 3 + name_len + 4 + 4 + 2
    if type_byte == FRAME_TYPE_DATA:
        length = struct.unpack("!H", bytes(header_bytes[6:8]))[0]
        if length > max_chunk_size:
            return None
        return 8 + length + 2
    return None


def _parse_frame(frame_bytes):
    if len(frame_bytes) < 2:
        return None
    body, crc_bytes = frame_bytes[:-2], frame_bytes[-2:]
    if _crc16(body) != struct.unpack("!H", bytes(crc_bytes))[0]:
        return None
    type_byte = body[0]
    file_id = body[1]
    if type_byte == FRAME_TYPE_DIRECTORY:
        name_len = body[2]
        name = bytes(body[3:3 + name_len]).decode("utf-8", errors="replace")
        total_size, checksum = struct.unpack("!II", bytes(body[3 + name_len:3 + name_len + 8]))
        return {
            "type": "directory", "file_id": file_id, "filename": name,
            "total_size": total_size, "checksum": checksum,
        }
    if type_byte == FRAME_TYPE_DATA:
        offset, length = struct.unpack("!IH", bytes(body[2:8]))
        payload = bytes(body[8:8 + length])
        return {"type": "data", "file_id": file_id, "offset": offset, "payload": payload}
    return None


class FileBroadcastDeframer(gr.sync_block):
    # Hard cap on bits collected per in-progress frame before giving up and
    # resuming search, independent of max_chunk_size's own DATA-frame check
    # above -- guards a false sync match on a DIRECTORY-shaped header whose
    # bogus name_len is still within a byte's range but the block never
    # actually receives that many more real bits (a stalled/dead scanner is
    # a worse failure than an early abandon-and-resync).
    MAX_FRAME_BITS = 8 * (8 + 512 + 2)

    def __init__(self, on_frame, max_chunk_size):
        gr.sync_block.__init__(self, name="FileBroadcastDeframer", in_sig=[np.uint8], out_sig=None)
        self._on_frame = on_frame
        self._max_chunk_size = max_chunk_size
        self._lock = threading.Lock()
        self._frame_count = 0
        self._crc_fail_count = 0
        self._reset()

    def _reset(self):
        self._window = []  # last len(_SYNC_BITS) bits seen while searching
        self._collecting = False
        self._bitacc = []
        self._need_total_bytes = None

    def work(self, input_items, output_items):
        bits = input_items[0]
        with self._lock:
            for bit in bits:
                bit = int(bit) & 1
                if not self._collecting:
                    self._window.append(bit)
                    if len(self._window) > len(_SYNC_BITS):
                        self._window.pop(0)
                    if len(self._window) == len(_SYNC_BITS) and self._window == _SYNC_BITS.tolist():
                        self._collecting = True
                        self._bitacc = []
                        self._need_total_bytes = None
                    continue

                self._bitacc.append(bit)
                if len(self._bitacc) >= self.MAX_FRAME_BITS:
                    self._reset()
                    continue
                if len(self._bitacc) % 8 != 0:
                    continue
                buf = np.packbits(np.array(self._bitacc, dtype=np.uint8), bitorder="big")
                n_bytes = len(buf)

                if self._need_total_bytes is None:
                    hlen = _header_len_for_type(int(buf[0]))
                    if hlen is None:
                        self._reset()
                        continue
                    if n_bytes >= hlen:
                        total = _total_frame_len(buf[:hlen], self._max_chunk_size)
                        if total is None:
                            self._reset()
                            continue
                        self._need_total_bytes = total

                if self._need_total_bytes is not None and n_bytes >= self._need_total_bytes:
                    frame_bytes = bytes(buf[:self._need_total_bytes])
                    frame = _parse_frame(frame_bytes)
                    if frame is not None:
                        self._frame_count += 1
                        self._on_frame(frame)
                    else:
                        self._crc_fail_count += 1
                    self._reset()
        return len(bits)

    @property
    def frame_count(self):
        with self._lock:
            return self._frame_count

    @property
    def crc_fail_count(self):
        with self._lock:
            return self._crc_fail_count
