"""Queue-fed GNU Radio source block for File Broadcast TX: pulls unpacked-
byte output from a filebroadcast.FileBroadcastPlanner's rotation cycle,
replacing Phase 1's fixed `blocks.vector_source_b(rotation_bytes,
repeat=True)` (rebuilt fresh in key_ptt() every Start press).

Why this exists (Phase 2's stated reason, see the plan): once more than one
file can be added/removed WHILE the flowgraph is running (the whole point of
a multi-file rotation the operator manages live), Phase 1's approach --
disconnect/rebuild vector_source_b -- would need a lock()/disconnect()/
connect() flowgraph pause on every file add/remove, not just on Start. This
block instead stays connected permanently (like mic_source/file_source in
FM/SSB mode, always running regardless of PTT -- tx_gain downstream is what
actually gates RF, same as every other mode) and rebuilds its OWN internal
rotation buffer from the planner -- but only at a SAFE boundary (the end of
the CURRENT rotation cycle), never mid-frame or mid-cycle, so a live file
add/remove never truncates or corrupts a frame already in flight.
"""
import threading

import numpy as np
from gnuradio import gr


class FileBroadcastSource(gr.sync_block):
    def __init__(self, planner, chunk_size):
        gr.sync_block.__init__(self, name="FileBroadcastSource", in_sig=None, out_sig=[np.uint8])
        self._planner = planner
        self._chunk_size = chunk_size
        self._lock = threading.Lock()
        self._rebuild_pending = False
        self._buf = np.zeros(0, dtype=np.uint8)
        self._pos = 0
        self._rebuild()

    def _rebuild(self):
        """Must be called with self._lock held. Empty planner -> a single
        harmless placeholder byte (matches Phase 1's own choice: keep
        transmitting something innocuous while unkeyed/idle rather than
        silence, which tx_gain's own mute already provides when actually
        unkeyed -- this is about what a curious "keyed but nothing loaded"
        state looks like, not a safety concern)."""
        rotation = self._planner.build_rotation_bytes(self._chunk_size)
        if not rotation:
            rotation = b"\x00"
        self._buf = np.frombuffer(rotation, dtype=np.uint8)
        self._pos = 0

    def request_rebuild(self):
        """Call from the app/GUI thread whenever the planner's file set
        changes (add_file/remove_file/clear). Thread-safe -- the actual
        rebuild happens lazily, inside work(), at the next safe rotation
        boundary (see work()'s own comment), not synchronously here."""
        with self._lock:
            self._rebuild_pending = True

    def work(self, input_items, output_items):
        out = output_items[0]
        n = len(out)
        produced = 0
        with self._lock:
            while produced < n:
                remaining = len(self._buf) - self._pos
                if remaining <= 0:
                    # End of one full rotation cycle -- the only point at
                    # which it's safe to pick up a pending rebuild (a file
                    # added/removed mid-cycle takes effect starting with
                    # the NEXT cycle, never interrupting a frame already
                    # in flight).
                    if self._rebuild_pending:
                        self._rebuild_pending = False
                    self._rebuild()
                    remaining = len(self._buf) - self._pos
                    if remaining <= 0:
                        break  # shouldn't happen (_rebuild always leaves >=1 byte), defensive only
                take = min(remaining, n - produced)
                out[produced:produced + take] = self._buf[self._pos:self._pos + take]
                self._pos += take
                produced += take
        return produced
