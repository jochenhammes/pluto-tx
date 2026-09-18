"""RX-side persistent Meshtastic traffic state -- structural mirror of
rtty_state.py (Qt/GNU-Radio-free, owned once by MainWindow, survives
AdvancedRxFlowgraph rebuilds, lock-guarded mutate / snapshot for the Qt
poll timer).

on_frame() is called from MeshtasticDeframer on the GNU Radio message
thread; the GUI polls get_snapshot() from the Qt thread and only re-renders
when `version` changed."""
import threading
import time

from pluto_tx import meshtastic_codec

MAX_ROWS = 500


class MeshtasticState:
    def __init__(self):
        self._lock = threading.Lock()
        self._rows = []  # oldest first
        self._version = 0
        # candidate (channel_name, psk) pairs a frame is tried against, in order
        self._channels = [(meshtastic_codec.DEFAULT_CHANNEL_NAME, meshtastic_codec.DEFAULT_CHANNEL_PSK)]

    def set_channels(self, channels):
        """channels: list of (name, psk_bytes). The first whose header channel
        hash matches decodes the frame; none matching -> shown as another
        channel (its payload can't be read without the right key)."""
        with self._lock:
            self._channels = list(channels)

    def on_frame(self, raw: bytes, now=None):
        with self._lock:
            channels = list(self._channels)
        row = None
        for name, psk in channels:
            summary = meshtastic_codec.summarize_packet(raw, psk=psk, channel_name=name)
            if summary.get("channel_match"):
                row = summary
                break
        if row is None:
            row = meshtastic_codec.summarize_packet(raw, psk=channels[0][1], channel_name=channels[0][0]) if channels else \
                meshtastic_codec.summarize_packet(raw)
            row["kind"] = "other channel" if "from" in row else "undecodable"
            row["text"] = ""
        row["time"] = time.time() if now is None else now
        with self._lock:
            self._rows.append(row)
            if len(self._rows) > MAX_ROWS:
                del self._rows[:len(self._rows) - MAX_ROWS]
            self._version += 1

    @property
    def version(self):
        with self._lock:
            return self._version

    def get_snapshot(self):
        with self._lock:
            return self._version, list(self._rows)

    def clear(self):
        with self._lock:
            self._rows.clear()
            self._version += 1
