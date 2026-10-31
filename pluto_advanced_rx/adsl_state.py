"""RX-side persistent ADS-L traffic state -- structural mirror of meshcore_state.py (Qt/GNU-Radio-free, owned once by
MainWindow, survives AdvancedRxFlowgraph rebuilds, lock-guarded mutate / snapshot for the Qt poll timer). Rows and the
aircraft list live in memory only; nothing is ever written to disk (positions of other people)."""
import threading
import time

from pluto_tx import adsl_codec

MAX_ROWS = 500
MAX_AIRCRAFT = 500
MAX_TRAIL_POINTS = 200
TRAFFIC_FIELDS = ("lat", "lon", "alt_m", "speed_mps", "climb_mps", "track_deg", "timestamp_s", "category", "flight_state",
                  "emergency")


class AdslState:
    def __init__(self):
        self._lock = threading.Lock()
        self._rows = []  # oldest first
        self._aircraft = {}  # address label -> latest known state
        self._version = 0

    @property
    def version(self):
        with self._lock:
            return self._version

    def on_packet(self, info, now=None):
        row = dict(info)
        row["time"] = time.time() if now is None else now
        with self._lock:
            self._rows.append(row)
            del self._rows[:-MAX_ROWS]
            if "address" in row:
                self._update_aircraft(row)
            self._version += 1
        return row

    def _update_aircraft(self, row):
        label = adsl_codec.address_label(row)
        entry = self._aircraft.get(label)
        if entry is None:
            if len(self._aircraft) >= MAX_AIRCRAFT:  # drop the longest-silent one
                del self._aircraft[min(self._aircraft, key=lambda k: self._aircraft[k]["time"])]
            entry = self._aircraft[label] = {"label": label, "packets": 0, "kinds": set(), "trail": []}
        entry["packets"] += 1
        entry["time"] = row["time"]
        entry["channel_hz"] = row.get("channel_hz")
        entry["frequency_offset_hz"] = row.get("frequency_offset_hz")
        entry["kinds"].add(row["kind"])
        for field in TRAFFIC_FIELDS:
            # A field the sender marks invalid (None) must not keep an older value alive; a Status packet has no
            # such fields at all and leaves the last known traffic data in place.
            if field in row:
                entry[field] = row[field]
        if row.get("lat") is not None and row.get("lon") is not None:
            entry["trail"].append([row["time"], row["lat"], row["lon"]])
            del entry["trail"][:-MAX_TRAIL_POINTS]

    def get_snapshot(self):
        with self._lock:
            return self._version, list(self._rows)

    def get_aircraft(self):
        """Most recently heard first."""
        with self._lock:
            return sorted((dict(a, kinds=set(a["kinds"]), trail=list(a["trail"])) for a in self._aircraft.values()),
                          key=lambda a: a["time"], reverse=True)

    def get_aircraft_json(self):
        """get_aircraft() with JSON-ready values, for a client joining later."""
        return [dict(a, kinds=sorted(a["kinds"])) for a in self.get_aircraft()]

    def counts(self):
        with self._lock:
            return len(self._rows), len(self._aircraft)

    def clear(self):
        with self._lock:
            self._rows = []
            self._aircraft = {}
            self._version += 1


def summarize(row):
    """One-line content of a packet for the log table."""
    kind = row["kind"]
    if kind != "traffic" or "alt_m" not in row:
        return kind
    position = "no position" if row["lat"] is None else f"{row['lat']:.5f}, {row['lon']:.5f}"
    altitude = "alt ?" if row["alt_m"] is None else f"{row['alt_m']} m"
    speed = "speed ?" if row["speed_mps"] is None else f"{row['speed_mps'] * 3.6:.0f} km/h"
    return f"{position}, {altitude}, {speed}, {row['category']}" + (", relayed" if row.get("relayed") else "")
