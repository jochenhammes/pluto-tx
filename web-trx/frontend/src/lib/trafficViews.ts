// What the aircraft panel shows per mode. ADS-L reports metres over the ellipsoid, ADS-B what aviation uses: feet, knots.
import { bearingDeg, distanceKm, type LatLon } from "./geo";

export type TrafficAircraft = {
  label: string; heard_ms: number; position_ms: number | null; trail: [number, number, number][];
  lat?: number | null; lon?: number | null; track_deg?: number | null; heading_deg?: number | null; emergency?: string;
  category?: string; alt_m?: number | null; speed_mps?: number | null; climb_mps?: number | null; packets?: number;
  callsign?: string; icao?: string; alt_ft?: number | null; alt_type?: string; speed_kt?: number | null; vrate_fpm?: number | null;
  on_ground?: boolean; squawk?: string; messages?: number;
};
export type TrafficColumn = {
  title: string; tooltip?: string; value: (a: TrafficAircraft, station: LatLon | null, now: number) => string;
};
export type TrafficView = {
  columns: TrafficColumn[]; rings: number[]; emptyText: string; tagText: (a: TrafficAircraft) => string;
};

const fixed = (v: number | null | undefined, digits = 0, scale = 1): string => (v == null ? "" : (v * scale).toFixed(digits));
const signed = (v: number | null | undefined, digits = 1): string => (v == null ? "" : `${v > 0 ? "+" : ""}${v.toFixed(digits)}`);
const position = (a: TrafficAircraft): LatLon | null => (a.lat != null && a.lon != null ? [a.lat, a.lon] : null);
const distance: TrafficColumn = {
  title: "km",
  value: (a, station) => { const p = position(a); return p && station ? distanceKm(station, p).toFixed(1) : ""; },
};
const bearing: TrafficColumn = {
  title: "Peilung",
  value: (a, station) => { const p = position(a); return p && station ? bearingDeg(station, p).toFixed(0) : ""; },
};
const age: TrafficColumn = { title: "Alter s", value: (a, _s, now) => String(Math.max(0, Math.round((now - a.heard_ms) / 1000))) };

export const ADSL_VIEW: TrafficView = {
  rings: [25, 50, 100],
  emptyText: "Noch keine Pakete empfangen.",
  tagText: (a) => (a.alt_m == null ? a.label : `${a.label} · ${Math.round(a.alt_m)} m`),
  columns: [
    { title: "Adresse", value: (a) => a.label },
    { title: "Kategorie", value: (a) => a.category ?? "" },
    { title: "Höhe m (Ellipsoid)", tooltip: "Über dem WGS-84-Ellipsoid, nicht über Grund oder Meer", value: (a) => fixed(a.alt_m) },
    { title: "km/h", value: (a) => fixed(a.speed_mps, 0, 3.6) },
    { title: "m/s", value: (a) => signed(a.climb_mps) },
    { title: "Kurs", value: (a) => fixed(a.track_deg) },
    distance, bearing, age,
    { title: "Pakete", value: (a) => String(a.packets ?? 0) },
  ],
};

export const ADSB_VIEW: TrafficView = {
  rings: [50, 100, 200, 400],
  emptyText: "Noch keine Flugzeuge empfangen. Eine Position braucht zwei passende Nachrichten.",
  tagText: (a) => `${a.callsign || a.label}${a.alt_ft == null ? "" : ` · ${Math.round(a.alt_ft)} ft`}`,
  columns: [
    { title: "ICAO", value: (a) => a.label },
    { title: "Rufzeichen", value: (a) => a.callsign ?? "" },
    { title: "Höhe ft", tooltip: "Wie gemeldet: meist barometrisch, GNSS ist gekennzeichnet",
      value: (a) => (a.alt_ft == null ? "" : `${Math.round(a.alt_ft)}${a.alt_type === "gnss" ? " GNSS" : ""}`) },
    { title: "kt", value: (a) => fixed(a.speed_kt) },
    { title: "ft/min", value: (a) => signed(a.vrate_fpm, 0) },
    { title: "Kurs", value: (a) => fixed(a.track_deg ?? a.heading_deg) },
    distance, bearing,
    { title: "Status", value: (a) => [a.on_ground ? "Boden" : "", a.emergency && a.emergency !== "none" ? a.emergency : "", a.squawk ?? ""].filter(Boolean).join(" ") },
    age,
    { title: "Nachr.", value: (a) => String(a.messages ?? 0) },
  ],
};
