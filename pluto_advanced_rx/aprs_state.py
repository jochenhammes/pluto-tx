"""RX-side APRS state (Qt/GNU-Radio-free, owned once by the application, survives flowgraph rebuilds, lock-guarded): the
stations heard with their latest position, a log of packets, and the text messages. Memory only; nothing is written to disk.
Only what actually came in over the air: this project has no APRS-IS connection."""
import threading
import time

from pluto_tx import aprs_codec

MAX_ROWS = 500
MAX_STATIONS = 1000
MAX_TRAIL_POINTS = 200
MAX_MESSAGES = 200
EXPIRE_S = 6 * 3600.0  # beacons can be half an hour apart, so a station stays much longer than an aircraft
_STATION_FIELDS = ("symbol", "comment", "course_deg", "speed_kt", "alt_ft", "temperature_c", "wind_dir_deg", "wind_mph",
                   "gust_mph", "humidity_pct", "pressure_hpa", "heard_via")


class AprsState:
    def __init__(self):
        self._lock = threading.Lock()
        self._rows = []
        self._stations = {}
        self._messages = []
        self._changed = set()
        self._version = 0

    @property
    def version(self):
        with self._lock:
            return self._version

    def on_frame(self, frame, now=None):
        """Returns the packet's fields, for the caller's own event."""
        now = time.time() if now is None else now
        row = dict(aprs_codec.summarize(frame), time=now)
        with self._lock:
            self._rows.append(row)
            del self._rows[:-MAX_ROWS]
            self._update_station(row, now)
            if row["kind"] in ("message", "ack", "reject", "bulletin"):
                self._messages.append(row)
                del self._messages[:-MAX_MESSAGES]
            self._version += 1
        return row

    def _update_station(self, row, now):
        # Objects and items have their own name and position; the sender is only their reporter.
        name = row.get("name") if row["kind"] in ("object", "item") else row.get("third_party_source") or row["source"]
        if not name:
            return
        entry = self._stations.get(name)
        if entry is None:
            if len(self._stations) >= MAX_STATIONS:
                del self._stations[min(self._stations, key=lambda k: self._stations[k]["time"])]
            entry = self._stations[name] = {"label": name, "packets": 0, "lat": None, "lon": None, "position_time": None, "trail": [],
                                            "kind": row["kind"]}
        entry["packets"] += 1
        entry["time"] = now
        if row["kind"] != "other":
            entry["kind"] = row["kind"]
        for field in _STATION_FIELDS:
            if field in row:
                entry[field] = row[field]
        if row["kind"] in ("object", "item"):
            entry["reported_by"] = row["source"]
            entry["alive"] = row.get("alive")
        if row.get("lat") is not None and row.get("lon") is not None:
            entry["lat"], entry["lon"] = row["lat"], row["lon"]
            entry["position_time"] = now
            entry["trail"].append([now, row["lat"], row["lon"]])
            del entry["trail"][:-MAX_TRAIL_POINTS]
        self._changed.add(name)

    def _expire(self, now):
        for name in [k for k, s in self._stations.items() if now - s["time"] > EXPIRE_S]:
            del self._stations[name]
            self._changed.discard(name)

    def get_stations(self, now=None):
        """Most recently heard first."""
        now = time.time() if now is None else now
        with self._lock:
            self._expire(now)
            rows = [dict(s, trail=list(s["trail"])) for s in self._stations.values()]
        return sorted(rows, key=lambda s: s["time"], reverse=True)

    def take_changed(self, now=None):
        now = time.time() if now is None else now
        with self._lock:
            self._expire(now)
            names, self._changed = self._changed, set()
            return [{k: v for k, v in self._stations[n].items() if k != "trail"} for n in names if n in self._stations]

    def get_rows(self):
        with self._lock:
            return list(self._rows)

    def get_messages(self):
        with self._lock:
            return list(self._messages)

    def counts(self):
        with self._lock:
            return len(self._rows), len(self._stations)

    def clear(self):
        with self._lock:
            self._rows, self._stations, self._messages, self._changed = [], {}, [], set()
            self._version += 1
