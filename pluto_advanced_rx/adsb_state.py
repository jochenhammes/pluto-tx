"""RX-side ADS-B aircraft list (Qt/GNU-Radio-free, owned once by the application, survives flowgraph rebuilds, lock-guarded).
Combines single messages into aircraft: positions need a pair of messages or a reference, so this is where they get
located. Memory only; nothing is ever written to disk. Monitoring only, never for navigation or collision avoidance."""
import threading
import time

from pluto_tx import adsb_codec

MAX_AIRCRAFT = 1000
MAX_TRAIL_POINTS = 200
MIN_MESSAGES = 2  # one lone message that passes the CRC may still be noise: not shown yet
EXPIRE_S = 900.0
CPR_PAIR_MAX_AGE_S = 10.0
LOCAL_REFERENCE_MAX_AGE_S = 60.0
MAX_SPEED_KM_S = 0.7  # about 1350 kt; anything faster between two fixes is a decoding error, not an aircraft
POSITION_MARGIN_KM = 5.0
MAX_RANGE_KM = 800.0  # far beyond the radio horizon of any airliner, so a global fix out here is wrong
_PUBLIC = ("callsign", "alt_ft", "alt_type", "speed_kt", "speed_type", "track_deg", "heading_deg", "vrate_fpm", "on_ground",
           "emergency", "squawk")


class AdsbState:
    def __init__(self, reference=None):
        self._lock = threading.Lock()
        self._aircraft = {}
        self._changed = set()
        self._version = 0
        self.reference = reference  # (lat, lon) of the receiver if known: surface positions and a range check
        self.positions_rejected = 0

    def set_reference(self, reference):
        with self._lock:
            self.reference = reference

    @property
    def version(self):
        with self._lock:
            return self._version

    def on_message(self, info, now=None):
        now = time.time() if now is None else now
        with self._lock:
            entry = self._aircraft.get(info["icao"])
            if entry is None:
                if len(self._aircraft) >= MAX_AIRCRAFT:
                    del self._aircraft[min(self._aircraft, key=lambda k: self._aircraft[k]["time"])]
                entry = self._aircraft[info["icao"]] = {"icao": info["icao"], "messages": 0, "trail": [], "lat": None, "lon": None,
                                                        "position_time": None, "_even": None, "_odd": None}
            entry["messages"] += 1
            entry["time"] = now
            for field in _PUBLIC:
                if field in info:
                    entry[field] = info[field]
            if "cpr_lat" in info:
                self._locate(entry, info, now)
            self._changed.add(info["icao"])
            self._version += 1

    def _locate(self, entry, info, now):
        odd = info["cpr_odd"]
        cpr = (info["cpr_lat"], info["cpr_lon"], now)
        entry["_odd" if odd else "_even"] = cpr
        surface = info["kind"] == "surface"
        fix = (entry["lat"], entry["lon"]) if entry["lat"] is not None else None
        fix_age = now - entry["position_time"] if fix else None
        position = None
        if fix and fix_age <= LOCAL_REFERENCE_MAX_AGE_S:
            position = adsb_codec.cpr_local(info["cpr_lat"], info["cpr_lon"], odd, fix, surface)
            if adsb_codec.distance_km(fix, position) > MAX_SPEED_KM_S * fix_age + POSITION_MARGIN_KM:
                self.positions_rejected += 1
                entry["lat"] = entry["lon"] = None  # start over from a fresh pair rather than trust either
                return
        elif surface:
            if self.reference is None:
                return
            position = adsb_codec.cpr_local(info["cpr_lat"], info["cpr_lon"], odd, self.reference, True)
        else:
            other = entry["_even" if odd else "_odd"]
            if other is None or now - other[2] > CPR_PAIR_MAX_AGE_S:
                return
            even_pair = entry["_even"][:2]
            odd_pair = entry["_odd"][:2]
            position = adsb_codec.cpr_global(even_pair, odd_pair, odd)
            if position is None:
                return
        if self.reference and adsb_codec.distance_km(self.reference, position) > MAX_RANGE_KM:
            self.positions_rejected += 1
            return
        entry["lat"], entry["lon"] = position
        entry["position_time"] = now
        entry["trail"].append([now, position[0], position[1]])
        del entry["trail"][:-MAX_TRAIL_POINTS]

    def _expire(self, now):
        for icao in [k for k, a in self._aircraft.items() if now - a["time"] > EXPIRE_S]:
            del self._aircraft[icao]
            self._changed.discard(icao)

    @staticmethod
    def _public(entry):
        return {k: (list(v) if k == "trail" else v) for k, v in entry.items() if not k.startswith("_")}

    def get_aircraft(self, now=None):
        """Everything heard often enough to be believed, most recently heard first."""
        now = time.time() if now is None else now
        with self._lock:
            self._expire(now)
            rows = [self._public(a) for a in self._aircraft.values() if a["messages"] >= MIN_MESSAGES]
        return sorted(rows, key=lambda a: a["time"], reverse=True)

    def take_changed(self, now=None):
        """The aircraft changed since the last call, for a periodic update: hundreds of messages a second must not
        mean hundreds of updates."""
        now = time.time() if now is None else now
        with self._lock:
            self._expire(now)
            names, self._changed = self._changed, set()
            return [self._public(self._aircraft[n]) for n in names
                    if n in self._aircraft and self._aircraft[n]["messages"] >= MIN_MESSAGES]

    def counts(self):
        with self._lock:
            return sum(1 for a in self._aircraft.values() if a["messages"] >= MIN_MESSAGES), self.positions_rejected

    def clear(self):
        with self._lock:
            self._aircraft = {}
            self._changed = set()
            self._version += 1
