"""RX-side persistent File Broadcast state: which files have been announced
by a directory frame, how much of each has actually been received (tracked
as a real sorted hole-interval list, not just a byte-level mask -- see
FileEntry below), and (once complete) the reconstructed bytes ready to save.
Deliberately Qt/GNU-Radio-free (same separation as fft_probe.py/digitext.py)
-- owned by MainWindow (gui.py), constructed ONCE at startup, so it survives
AdvancedRxFlowgraph rebuilds (bandwidth changes) by construction, exactly
like FftProbe's own survives-a-rebuild relationship to the flowgraph it's
re-attached to on each rebuild (see gui.py's _on_bandwidth_changed).

filebroadcast_deframer.FileBroadcastDeframer calls on_directory_frame()/
on_data_frame() directly from its work() method, on the GNU Radio scheduler
thread; the GUI polls get_snapshot() from the Qt thread. _lock guards every
access from both sides -- same "compute a plain-data snapshot under the
lock, return it lock-free" idiom already proven by FftProbe.get_latest_row().

Phase 3 scope (this module): RX-only PASSIVE hole-filling -- the receiver
just keeps listening across repeated blind rotation cycles (the sender is
stateless and has no idea who's listening or what they're missing) and
fills in whatever data frames it happens to catch, in any order, across any
number of cycles. There is deliberately no uplink/back-channel in this app
suite, so a real closed-loop request-based hole-fill (telling the sender
what's missing) is out of scope here, not an oversight.

Per-file record/don't-record selection (record_flag exists so Phase 4's
planned UI has somewhere to land, but stays True unconditionally for now --
everything directory-announced is recorded automatically) is still
deferred to Phase 4, unchanged from Phase 1/2.
"""
import threading
import time


def _subtract_interval(holes, start, end):
    """Remove the half-open range [start, end) from `holes`, a list of
    (offset, length) tuples representing a SORTED, NON-OVERLAPPING set of
    still-missing byte ranges. Returns a new sorted, non-overlapping list.

    Each hole overlapping [start, end) is trimmed or removed; a hole that
    is only PARTIALLY covered by [start, end) survives as whichever
    sub-range(s) remain outside it -- a hole entirely contained within
    [start, end) can split into two smaller holes (one on each side) if
    the newly-received chunk lands entirely inside a larger gap."""
    new_holes = []
    for h_off, h_len in holes:
        h_end = h_off + h_len
        if end <= h_off or start >= h_end:
            new_holes.append((h_off, h_len))  # no overlap with the received range
            continue
        if h_off < start:
            new_holes.append((h_off, start - h_off))  # leftover before the received range
        if end < h_end:
            new_holes.append((end, h_end - end))  # leftover after the received range
    return new_holes


class FileEntry:
    def __init__(self, filename, total_size, checksum):
        self.filename = filename
        self.total_size = total_size
        self.checksum = checksum
        self.data = bytearray(total_size)
        # Starts as one big hole covering the whole file -- shrinks via
        # _subtract_interval() as real data frames arrive, in whatever
        # order/across however many rotation cycles they happen to show up
        # in (passive gap-fill, see module docstring). A sorted list of
        # (offset, length) is far more compact than a byte-level mask once
        # a file is mostly received (a handful of small remaining holes vs.
        # one bit per byte), and directly matches the plan's own Phase 3
        # design ("sorted (offset, length) gaps... interval merge/split on
        # each received chunk").
        self.holes = [(0, total_size)] if total_size > 0 else []
        self.record_flag = True  # Phase 4 will make this operator-toggleable; always True for now
        self.first_seen_at = time.time()
        self.last_updated_at = time.time()

    def apply_chunk(self, offset, payload):
        end = min(offset + len(payload), self.total_size)
        n = end - offset
        if n <= 0:
            return
        self.data[offset:end] = payload[:n]
        self.holes = _subtract_interval(self.holes, offset, end)
        self.last_updated_at = time.time()

    @property
    def bytes_received(self):
        return self.total_size - sum(length for _, length in self.holes)

    @property
    def is_complete(self):
        return self.total_size > 0 and not self.holes


class FileBroadcastState:
    def __init__(self):
        self._lock = threading.Lock()
        self._files = {}  # file_id -> FileEntry

    def on_directory_frame(self, file_id, filename, total_size, checksum):
        with self._lock:
            entry = self._files.get(file_id)
            if entry is None or entry.total_size != total_size or entry.filename != filename:
                # New file, or the sender restarted this file_id with
                # different metadata (different file re-broadcast under
                # the same slot) -- start tracking it fresh (a fresh
                # all-holes state) rather than trying to reconcile a
                # mismatched size/name against already-buffered data from
                # a previous, different file.
                entry = FileEntry(filename, total_size, checksum)
                self._files[file_id] = entry
            entry.checksum = checksum
            entry.last_updated_at = time.time()

    def on_data_frame(self, file_id, offset, payload):
        with self._lock:
            entry = self._files.get(file_id)
            if entry is None:
                return  # data frame arrived before its directory frame this cycle -- drop, catches up next rotation
            entry.apply_chunk(offset, payload)

    def get_snapshot(self):
        """Plain-data list, safe to use outside the lock (matches
        FftProbe.get_latest_row()'s own always-a-fresh-copy contract).
        `holes` is included as a plain list copy (not the live list) --
        useful for a future debug/progress view (e.g. a per-file gap bar),
        not otherwise used by Phase 3's own logic."""
        with self._lock:
            return [
                {
                    "file_id": fid, "filename": e.filename, "total_size": e.total_size,
                    "bytes_received": e.bytes_received, "is_complete": e.is_complete,
                    "record_flag": e.record_flag, "last_updated_at": e.last_updated_at,
                    "holes": list(e.holes),
                }
                for fid, e in sorted(self._files.items())
            ]

    def is_complete(self, file_id):
        with self._lock:
            entry = self._files.get(file_id)
            return entry is not None and entry.is_complete

    def save_to_disk(self, file_id, path):
        with self._lock:
            entry = self._files.get(file_id)
            if entry is None:
                return False
            data = bytes(entry.data)
        with open(path, "wb") as f:
            f.write(data)
        return True

    def clear(self):
        with self._lock:
            self._files.clear()
