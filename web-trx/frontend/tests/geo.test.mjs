// npm test. Not every Node.js can run TypeScript itself, so geo.ts goes through esbuild (comes with vite).
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import { transformSync } from "esbuild";

const source = readFileSync(new URL("../src/lib/geo.ts", import.meta.url), "utf8");
const { code } = transformSync(source, { loader: "ts", format: "esm" });
const { bearingDeg, distanceKm, locatorCenter } = await import(`data:text/javascript,${encodeURIComponent(code)}`);

test("locator centre", () => {
  assert.deepEqual(locatorCenter("JO43"), [53.5, 9]);
  assert.deepEqual(locatorCenter("jo31"), [51.5, 7]);
  assert.deepEqual(locatorCenter("AA00"), [-89.5, -179]);
  assert.deepEqual(locatorCenter("JO43AA"), [53 + 0.5 / 24, 8 + 0.5 / 12]);
  assert.equal(locatorCenter(""), null);
  assert.equal(locatorCenter("nonsense"), null);
});

test("distance and bearing", () => {
  assert.ok(Math.abs(distanceKm([0, 0], [0, 1]) - 111.19) < 0.05); // one degree on the equator
  assert.ok(Math.abs(distanceKm([53.5, 9], [52.52, 13.405]) - 310) < 5); // about Hamburg to Berlin
  assert.equal(distanceKm([50, 8], [50, 8]), 0);
  assert.ok(Math.abs(bearingDeg([0, 0], [1, 0]) - 0) < 1e-6);
  assert.ok(Math.abs(bearingDeg([0, 0], [0, 1]) - 90) < 1e-6);
  assert.ok(Math.abs(bearingDeg([1, 0], [0, 0]) - 180) < 1e-6);
  assert.ok(Math.abs(bearingDeg([0, 1], [0, 0]) - 270) < 1e-6);
});
