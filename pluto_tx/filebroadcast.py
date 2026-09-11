"""Pure-function frame encoder + round-robin rotation planner for the
"File Broadcast" mode: a private-link (this app suite's own TX/RX only, not
real PACSAT interop -- see the plan/briefing) repetitive file-broadcast
scheme inspired by the PACSAT Broadcast Protocol. A custom, minimal
"PFH-lite" frame format replaces real AX.25/PACSAT wire framing (dropped
along with gr-satellites, per the locked-in "private link only" scope
decision) -- hand-rolling sync-word + CRC-16 framing is simpler than
adapting an AX.25-specific deframer to a non-AX.25 protocol anyway, once
real interop isn't a goal.

Deliberately Qt/GNU-Radio-free -- same separation as digitext.py/
fft_probe.py -- so the whole protocol is testable with synthetic bytes
alone, no hardware or running flowgraph needed. `pluto_advanced_rx`'s own
deframer (filebroadcast_deframer.py) is a SEPARATE, independent copy of the
frame-parsing half of this module (own CRC-16/header-length helpers), not an
import of this file -- matches this project's established convention for
PHY/protocol logic that must stay in sync across the two independent app
packages (see pluto_advanced_rx/config.py's module docstring, and e.g.
rade_ctypes.py's own independent per-package copies). A comment on both
sides flags this explicitly so they don't silently drift.

Frame format (all multi-byte integers big-endian, all fields byte-aligned --
no bit-packing beyond the GFSK PHY layer itself):

    SYNC_WORD (4 bytes, fixed)
    type (1 byte): FRAME_TYPE_DIRECTORY or FRAME_TYPE_DATA
    file_id (1 byte)
    --- DIRECTORY frame only ---
    name_len (1 byte)
    filename (name_len bytes, UTF-8)
    total_size (4 bytes)
    checksum (4 bytes, zlib.crc32 of the whole file)
    --- DATA frame only ---
    offset (4 bytes)
    length (2 bytes)
    payload (length bytes)
    --- both ---
    crc16 (2 bytes, CRC-16/CCITT-FALSE of every byte from `type` through
           the end of the payload/checksum field, i.e. NOT including
           SYNC_WORD or the crc16 field itself)

The receiver has no inherent byte alignment in a raw demodulated bit stream
(digital.gfsk_demod outputs one bit per byte, unpacked, no framing of its
own) -- SYNC_WORD exists purely to let the deframer find frame starts via a
bit-by-bit sliding-window search; `header_len_for_type`/`total_frame_len`
then let it determine exactly how many more bytes to collect once enough of
the type-specific header has arrived, without needing a fixed frame size.

Bit order: gfsk_mod(do_unpack=True) unpacks each input BYTE into 8 bits
MSB-first before modulating; digital.gfsk_demod's raw bit output stream
correspondingly matches np.unpackbits(byte, bitorder="big") bit-for-bit --
confirmed empirically during Phase 0's real-hardware PHY testing (the test
harness's own known-bits comparison used exactly this convention throughout
and matched cleanly). The deframer must repack recovered bits the same way
(np.packbits(bits, bitorder="big")) to recover the original bytes.
"""
import struct
import time
import zlib

SYNC_WORD = bytes.fromhex("1ACFFC1D")  # arbitrary but fixed 4-byte low-autocorrelation pattern

FRAME_TYPE_DIRECTORY = 0x01
FRAME_TYPE_DATA = 0x02

MAX_FILENAME_LEN = 64  # name_len is 1 byte, but this is a saner practical cap


def crc16(data: bytes) -> int:
    """CRC-16/CCITT-FALSE (poly 0x1021, init 0xFFFF) -- a plain, dependency-
    free implementation (no external crc library needed for a tiny protocol
    like this)."""
    crc = 0xFFFF
    for b in data:
        crc ^= b << 8
        for _ in range(8):
            if crc & 0x8000:
                crc = ((crc << 1) ^ 0x1021) & 0xFFFF
            else:
                crc = (crc << 1) & 0xFFFF
    return crc


def header_len_for_type(type_byte):
    """Bytes needed (after SYNC_WORD) to know which type-specific header
    fields are present -- for DIRECTORY that's type+file_id+name_len (3,
    since name_len itself determines the rest); for DATA it's
    type+file_id+offset+length (8, since length determines the payload
    size). Returns None for an unrecognized type byte (garbage/false sync
    match -- the deframer abandons and resumes searching)."""
    if type_byte == FRAME_TYPE_DIRECTORY:
        return 3
    if type_byte == FRAME_TYPE_DATA:
        return 8
    return None


def total_frame_len(header_bytes, max_chunk_size=None):
    """header_bytes: at least header_len_for_type(header_bytes[0]) bytes,
    collected right after SYNC_WORD. Returns the TOTAL frame length in
    bytes (from `type` through `crc16` inclusive, i.e. NOT counting
    SYNC_WORD), or None if the type byte is unrecognized.

    max_chunk_size: if given, a DATA frame declaring a payload length
    larger than this is treated as corrupt (returns None) rather than
    trusting an unverified length field straight out of noise -- a real
    length field read from a false/corrupted sync match could otherwise
    claim up to 65535 bytes and leave the deframer waiting indefinitely for
    a frame that will never legitimately complete."""
    type_byte = header_bytes[0]
    if type_byte == FRAME_TYPE_DIRECTORY:
        name_len = header_bytes[2]
        return 3 + name_len + 4 + 4 + 2  # + total_size(4) + checksum(4) + crc16(2)
    if type_byte == FRAME_TYPE_DATA:
        length = struct.unpack("!H", header_bytes[6:8])[0]
        if max_chunk_size is not None and length > max_chunk_size:
            return None
        return 8 + length + 2  # + payload(length) + crc16(2)
    return None


def build_directory_frame(file_id, filename, total_size, checksum):
    name_bytes = filename.encode("utf-8")[:MAX_FILENAME_LEN]
    body = (
        struct.pack("!BBB", FRAME_TYPE_DIRECTORY, file_id, len(name_bytes))
        + name_bytes
        + struct.pack("!II", total_size, checksum)
    )
    return SYNC_WORD + body + struct.pack("!H", crc16(body))


def build_data_frame(file_id, offset, payload):
    body = struct.pack("!BBIH", FRAME_TYPE_DATA, file_id, offset, len(payload)) + payload
    return SYNC_WORD + body + struct.pack("!H", crc16(body))


def parse_frame(frame_bytes):
    """frame_bytes: the frame body + crc16, i.e. total_frame_len() bytes,
    NOT including SYNC_WORD (the deframer strips it before calling this,
    since SYNC_WORD is only used to find frame starts, not carried as part
    of the length-determined body). Returns a dict describing the frame, or
    None if the CRC-16 check fails (a normal, expected outcome on a lossy
    real RF link -- not an error) or the type is unrecognized."""
    if len(frame_bytes) < 2:
        return None
    body, crc_bytes = frame_bytes[:-2], frame_bytes[-2:]
    if crc16(body) != struct.unpack("!H", crc_bytes)[0]:
        return None
    type_byte = body[0]
    file_id = body[1]
    if type_byte == FRAME_TYPE_DIRECTORY:
        name_len = body[2]
        name = body[3:3 + name_len].decode("utf-8", errors="replace")
        total_size, checksum = struct.unpack("!II", body[3 + name_len:3 + name_len + 8])
        return {
            "type": "directory", "file_id": file_id, "filename": name,
            "total_size": total_size, "checksum": checksum,
        }
    if type_byte == FRAME_TYPE_DATA:
        offset, length = struct.unpack("!IH", body[2:8])
        payload = bytes(body[8:8 + length])
        return {"type": "data", "file_id": file_id, "offset": offset, "payload": payload}
    return None


def chunk_bytes(data: bytes, chunk_size: int):
    return [data[i:i + chunk_size] for i in range(0, len(data), chunk_size)] or [b""]


class FileBroadcastPlanner:
    """Holds the set of files currently in rotation and builds the actual
    bytes to transmit. Pure round-robin at chunk granularity across every
    loaded file (the locked-in Phase 1+ scope decision -- see the plan's
    Context section) -- even though Phase 1 only ever loads one file at a
    time, this already produces the Phase 2 shape (multiple files'
    directory + data frames interleaved in one rotation), so Phase 2 only
    needs to let more than one file be added, not a planner rewrite.

    build_rotation_bytes() flattens one full rotation cycle into a single
    bytes buffer, meant to be looped by a GNU Radio vector_source_b with
    repeat=True (Phase 1's chosen TX source, see the plan) -- Phase 2's
    planned switch to a queue-fed custom source block will want a pull-style
    method instead (e.g. next_frame_bytes()); build_rotation_bytes() stays
    useful even then as the thing that pull interface iterates over, so it's
    not wasted work."""

    def __init__(self):
        self._files = {}  # file_id -> {"filename", "data", "checksum", "added_at"}
        self._next_file_id = 0

    def add_file(self, filename, data: bytes):
        file_id = self._next_file_id
        self._next_file_id = (self._next_file_id + 1) % 256
        self._files[file_id] = {
            "filename": filename,
            "data": bytes(data),
            "checksum": zlib.crc32(data) & 0xFFFFFFFF,
            "added_at": time.time(),
        }
        return file_id

    def remove_file(self, file_id):
        self._files.pop(file_id, None)

    def clear(self):
        self._files.clear()

    def files(self):
        return dict(self._files)

    def build_rotation_bytes(self, chunk_size: int):
        """One directory frame per file, then every file's data frames
        interleaved round-robin at chunk granularity (file A chunk 0, file B
        chunk 0, file A chunk 1, file B chunk 1, ... -- not "finish file A
        then start file B", so a receiver joining mid-cycle doesn't wait
        arbitrarily long to see any one file's directory entry, per the
        plan's Phase 2 rationale, already followed here in Phase 1's own
        single-file case for free)."""
        if not self._files:
            return b""
        frames = [
            build_directory_frame(fid, e["filename"], len(e["data"]), e["checksum"])
            for fid, e in self._files.items()
        ]
        chunked = {fid: chunk_bytes(e["data"], chunk_size) for fid, e in self._files.items()}
        max_chunks = max(len(c) for c in chunked.values())
        for i in range(max_chunks):
            for fid, chunks in chunked.items():
                if i < len(chunks):
                    frames.append(build_data_frame(fid, i * chunk_size, chunks[i]))
        return b"".join(frames)
