"""RX-side persistent MeshCore traffic state -- structural mirror of meshtastic_state.py (Qt/GNU-Radio-free,
owned once by MainWindow, survives AdvancedRxFlowgraph rebuilds, lock-guarded mutate / snapshot for the Qt
poll timer). Rows and the node list live in memory only; nothing is ever written to disk (other people's
messages)."""
import threading
import time

from pluto_tx import meshcore_codec

MAX_ROWS = 500


class MeshcoreState:
    def __init__(self):
        self._lock = threading.Lock()
        self._rows = []           # oldest first
        self._nodes = {}          # public key hex -> latest advert info
        self._version = 0
        self._channels = [meshcore_codec.PUBLIC_CHANNEL]
        self._identity = None     # this app's node identity: direct messages addressed to it are decrypted
        self.hide_unverified = False  # rows that could not authenticate themselves (may be CRC-damaged frames)

    def set_channels(self, channels):
        """channels: list of GroupChannel; the Public channel is always kept."""
        with self._lock:
            extra = [c for c in channels if c.secret != meshcore_codec.PUBLIC_CHANNEL_SECRET]
            self._channels = [meshcore_codec.PUBLIC_CHANNEL] + extra
            self._version += 1

    def set_identity(self, identity):
        """Identity whose direct messages to decrypt (None = do not decrypt any). Only messages addressed to this
        node can ever be read; the senders' keys come from the adverts heard so far."""
        with self._lock:
            self._identity = identity
            self._version += 1

    def set_hide_unverified(self, hide):
        with self._lock:
            self.hide_unverified = bool(hide)
            self._version += 1

    @property
    def version(self):
        with self._lock:
            return self._version

    def on_frame(self, raw: bytes, now=None):
        with self._lock:
            channels = list(self._channels)
            identity = self._identity
            peers = [bytes.fromhex(k) for k in self._nodes]
        info = meshcore_codec.summarize_packet(raw, channels, identity, peers)
        info["time"] = time.time() if now is None else now
        with self._lock:
            self._rows.append(info)
            del self._rows[:-MAX_ROWS]
            if info["kind"] == "advert" and info["verified"]:
                self._nodes[info["public_key"]] = info
                self._retry_pending_direct_messages()
            self._version += 1
        return info

    def _retry_pending_direct_messages(self):
        """A direct message can arrive before its sender's advert; decrypt those rows once the key is known."""
        if self._identity is None:
            return
        peers = [bytes.fromhex(k) for k in self._nodes]
        for i, row in enumerate(self._rows):
            if row["kind"] == "other" and row.get("for_this_node") and not row["verified"]:
                fresh = meshcore_codec.summarize_packet(bytes.fromhex(row["hex"]), self._channels, self._identity, peers)
                if fresh["kind"] == "direct_text":
                    fresh["time"] = row["time"]
                    self._rows[i] = fresh

    def clear(self):
        with self._lock:
            self._rows.clear()
            self._nodes.clear()
            self._version += 1

    def get_snapshot(self):
        """-> (version, rows); each row is a summarize_packet() dict plus 'time'. Rows that failed to parse are
        never shown; unverified 'other' rows can be hidden."""
        with self._lock:
            rows = [r for r in self._rows if r["kind"] != "invalid"
                    and not (self.hide_unverified and not r["verified"])]
            return self._version, list(rows)

    def counts(self):
        """-> (frames received, invalid/damaged, hidden in the table)"""
        with self._lock:
            invalid = sum(1 for r in self._rows if r["kind"] == "invalid")
            hidden = invalid + (sum(1 for r in self._rows if r["kind"] != "invalid" and not r["verified"])
                                if self.hide_unverified else 0)
            return len(self._rows), invalid, hidden

    def get_nodes(self):
        with self._lock:
            return sorted(self._nodes.values(), key=lambda r: r["time"], reverse=True)
