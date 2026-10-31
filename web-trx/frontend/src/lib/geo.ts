// Small geodesy helpers for the ADS-L map. Plain functions, no dependencies.

export type LatLon = [number, number];

const LOCATOR_RE = /^[A-R]{2}[0-9]{2}([A-X]{2})?$/;
const EARTH_RADIUS_KM = 6371;
const rad = (deg: number): number => (deg * Math.PI) / 180;

/** Middle of a Maidenhead locator (4 or 6 characters), north and east positive; null if empty or invalid. */
export function locatorCenter(locator: string): LatLon | null {
  const l = locator.trim().toUpperCase();
  if (!LOCATOR_RE.test(l)) return null;
  const lon = (l.charCodeAt(0) - 65) * 20 + Number(l[2]) * 2 - 180;
  const lat = (l.charCodeAt(1) - 65) * 10 + Number(l[3]) - 90;
  if (l.length === 6) return [lat + (l.charCodeAt(5) - 65 + 0.5) / 24, lon + (l.charCodeAt(4) - 65 + 0.5) / 12];
  return [lat + 0.5, lon + 1];
}

/** Great-circle distance in km. */
export function distanceKm(a: LatLon, b: LatLon): number {
  const dLat = rad(b[0] - a[0]);
  const dLon = rad(b[1] - a[1]);
  const h = Math.sin(dLat / 2) ** 2 + Math.cos(rad(a[0])) * Math.cos(rad(b[0])) * Math.sin(dLon / 2) ** 2;
  return 2 * EARTH_RADIUS_KM * Math.asin(Math.min(1, Math.sqrt(h)));
}

/** Initial bearing from a to b in degrees, clockwise from true north. */
export function bearingDeg(a: LatLon, b: LatLon): number {
  const dLon = rad(b[1] - a[1]);
  const y = Math.sin(dLon) * Math.cos(rad(b[0]));
  const x = Math.cos(rad(a[0])) * Math.sin(rad(b[0])) - Math.sin(rad(a[0])) * Math.cos(rad(b[0])) * Math.cos(dLon);
  return ((Math.atan2(y, x) * 180) / Math.PI + 360) % 360;
}
