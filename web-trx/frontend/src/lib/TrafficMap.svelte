<script lang="ts">
  import L from "leaflet";
  import "leaflet/dist/leaflet.css";
  import { onDestroy, onMount } from "svelte";
  import type { TrafficAircraft } from "./trafficViews";
  import type { LatLon } from "./geo";

  // Live map of the aircraft heard, for ADS-L and ADS-B. Leaflet is bundled; only the background tiles come from outside
  // (from the browser, see tileSetting), and everything drawn here works without them.

  export let aircraft: TrafficAircraft[] = [];
  export let rings: number[] = [];
  export let tagText: (a: TrafficAircraft) => string = (a) => a.label;
  export let station: LatLon | null = null;
  export let selected: string | null = null;
  export let onSelect: (label: string | null) => void = () => {};
  /** features.map_tile_url: "" = OpenStreetMap, "none" = no background, otherwise a tile URL template */
  export let tileSetting = "";
  /** Local time of the last refresh (ms), so that markers age without new packets. */
  export let now = Date.now();

  const OSM_TILES = "https://tile.openstreetmap.org/{z}/{x}/{y}.png";
  const OSM_ATTRIBUTION = '&copy; <a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noreferrer">OpenStreetMap</a>';
  const STALE_MS = 60_000; // faded after this long without a position
  const GONE_MS = 300_000; // off the map after this long; the table keeps the entry
  const KM_PER_DEGREE_LAT = 111.2;
  const RING_LABELS_MIN_ZOOM = 7; // zoomed out further they all fall on top of the station
  const DEFAULT_VIEW: [LatLon, number] = [[51.2, 10.4], 6];

  type Drawn = { marker: L.Marker; trail: L.Polyline; html: string };

  let container: HTMLDivElement;
  let map: L.Map | undefined;
  let observer: ResizeObserver | undefined;
  let stationLayer: L.LayerGroup | undefined;
  let drawnStation: LatLon | null = null;
  const drawn = new Map<string, Drawn>();
  let fitted = false;

  const escapeHtml = (s: string): string =>
    s.replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c] as string);

  function visible(a: TrafficAircraft): boolean {
    return a.lat != null && a.lon != null && a.position_ms != null && now - a.position_ms < GONE_MS;
  }

  function iconHtml(a: TrafficAircraft): string {
    const classes = ["traffic-craft"];
    if (a.emergency && a.emergency !== "none" && a.emergency !== "undefined") classes.push("emergency");
    if (now - (a.position_ms ?? 0) >= STALE_MS) classes.push("stale");
    if (a.label === selected) classes.push("selected");
    // Without a direction (not moving) a dot instead of an arrow pointing nowhere.
    const direction = a.track_deg ?? a.heading_deg;
    const glyph = direction == null
      ? '<circle cx="12" cy="12" r="5"/>'
      : `<path transform="rotate(${Math.round(direction)} 12 12)" d="M12 2 L19 21 L12 17 L5 21 Z"/>`;
    return `<div class="${classes.join(" ")}"><svg width="24" height="24" viewBox="0 0 24 24">${glyph}</svg>`
      + `<span class="traffic-tag">${escapeHtml(tagText(a))}</span></div>`;
  }

  function refresh(): void {
    if (!map) return;
    const seen = new Set<string>();
    for (const a of aircraft) {
      if (!visible(a)) continue;
      seen.add(a.label);
      const html = iconHtml(a);
      const position: LatLon = [a.lat as number, a.lon as number];
      const path = a.trail.map((p) => [p[1], p[2]] as LatLon);
      let d = drawn.get(a.label);
      if (!d) {
        const label = a.label;
        const marker = L.marker(position, { icon: L.divIcon({ html, className: "", iconSize: [24, 24], iconAnchor: [12, 12] }) })
          .on("click", () => onSelect(label))
          .addTo(map);
        d = { marker, trail: L.polyline(path, { weight: 2, opacity: 0.7, interactive: false }).addTo(map), html };
        drawn.set(label, d);
      } else {
        d.marker.setLatLng(position);
        if (d.html !== html) {
          d.marker.setIcon(L.divIcon({ html, className: "", iconSize: [24, 24], iconAnchor: [12, 12] }));
          d.html = html;
        }
        d.trail.setLatLngs(path);
      }
      d.trail.setStyle({ color: html.includes("emergency") ? "#ff4d4d" : "#3ea6ff" });
    }
    for (const [label, d] of drawn) {
      if (seen.has(label)) continue;
      d.marker.remove();
      d.trail.remove();
      drawn.delete(label);
    }
    if (!fitted && seen.size > 0) {
      fitted = true;
      fitAll();
    }
  }

  function drawStation(): void {
    if (!map || drawnStation === station) return;
    drawnStation = station;
    stationLayer?.remove();
    stationLayer = undefined;
    if (!station) return;
    const layer = L.layerGroup();
    for (const km of rings) {
      L.circle(station, { radius: km * 1000, color: "#8993a8", weight: 1, dashArray: "4 6", fill: false, interactive: false })
        .addTo(layer);
      L.marker([station[0] + km / KM_PER_DEGREE_LAT, station[1]], {
        icon: L.divIcon({ html: `<span class="traffic-ring-label">${km} km</span>`, className: "", iconSize: [50, 14], iconAnchor: [25, 7] }),
        interactive: false,
      }).addTo(layer);
    }
    L.marker(station, {
      icon: L.divIcon({ html: '<div class="traffic-station"></div>', className: "", iconSize: [14, 14], iconAnchor: [7, 7] }),
      title: "Eigene Station",
      interactive: false,
    }).addTo(layer);
    stationLayer = layer.addTo(map);
    if (!fitted) map.setView(station, 8);
  }

  /** Shows every aircraft on the map, plus the own station. Only on request, so the map never jumps away. */
  export function fitAll(): void {
    if (!map) return;
    const points = aircraft.filter(visible).map((a) => [a.lat, a.lon] as LatLon);
    if (station) points.push(station);
    if (points.length === 0) return;
    map.fitBounds(L.latLngBounds(points), { padding: [40, 40], maxZoom: 11 });
  }

  export function resize(): void {
    map?.invalidateSize();
  }

  onMount(() => {
    map = L.map(container, { attributionControl: true, worldCopyJump: true });
    map.attributionControl.setPrefix("Leaflet");
    map.setView(DEFAULT_VIEW[0], DEFAULT_VIEW[1]);
    if (tileSetting !== "none") {
      L.tileLayer(tileSetting || OSM_TILES, {
        maxZoom: 18,
        attribution: tileSetting ? "" : OSM_ATTRIBUTION,
      }).addTo(map);
    }
    map.on("click", () => onSelect(null));
    const showRingLabels = () => container.classList.toggle("far", (map?.getZoom() ?? 0) < RING_LABELS_MIN_ZOOM);
    map.on("zoomend", showRingLabels);
    showRingLabels();
    observer = new ResizeObserver(() => map?.invalidateSize());
    observer.observe(container);
    drawStation();
    refresh();
  });

  onDestroy(() => {
    observer?.disconnect();
    drawn.clear();
    map?.remove();
    map = undefined;
  });

  $: if (map) {
    void station;
    drawStation();
  }
  $: if (map) {
    void aircraft;
    void now;
    void selected;
    refresh();
  }
</script>

<div class="map" class:no-tiles={tileSetting === "none"} bind:this={container}></div>

<style>
  .map {
    width: 100%;
    height: 100%;
    min-height: 160px;
    background: #10141c; /* what shows where no tile could be loaded */
    border-radius: var(--radius);
  }
  /* Dark like the rest of the app; markers and labels are not affected. */
  .map :global(.leaflet-tile-pane) {
    filter: invert(1) hue-rotate(180deg) brightness(0.85) contrast(0.9);
  }
  .map :global(.leaflet-control-attribution) {
    background: rgba(10, 12, 17, 0.75);
    color: var(--text-dim);
  }
  .map :global(.leaflet-control-attribution a) {
    color: var(--accent);
  }
  .map :global(.leaflet-bar a) {
    background: var(--bg-2);
    color: var(--text);
    border-color: var(--border);
  }
  .map :global(.traffic-craft) {
    position: relative;
    width: 24px;
    height: 24px;
    color: #3ea6ff;
    cursor: pointer;
  }
  .map :global(.traffic-craft svg) {
    fill: currentColor;
    stroke: #0a0c11;
    stroke-width: 1.2;
    display: block;
  }
  .map :global(.traffic-craft.emergency) {
    color: #ff4d4d;
  }
  .map :global(.traffic-craft.stale) {
    opacity: 0.45;
  }
  .map :global(.traffic-craft.selected) {
    color: #ffd166;
  }
  .map :global(.traffic-tag) {
    position: absolute;
    left: 26px;
    top: 3px;
    white-space: nowrap;
    font: 11px var(--mono);
    padding: 0 4px;
    color: var(--text);
    background: rgba(10, 12, 17, 0.75);
    border-radius: 3px;
  }
  .map :global(.traffic-station) {
    width: 14px;
    height: 14px;
    background: var(--ok);
    border: 2px solid #0a0c11;
    border-radius: 50%;
  }
  .map:global(.far) :global(.traffic-ring-label) {
    display: none;
  }
  .map :global(.traffic-ring-label) {
    display: block;
    text-align: center;
    color: var(--text-dim);
    font-size: 10px;
    line-height: 14px;
    pointer-events: none;
  }
</style>
