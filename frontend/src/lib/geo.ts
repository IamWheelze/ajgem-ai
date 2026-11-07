/** Small, dependency-free geometry helpers for the map workspace. */

/** Approximate circle polygon (display only) for a positional uncertainty radius in metres. */
export function circlePolygon(lon: number, lat: number, radiusM: number, steps = 48): GeoJSON.Polygon {
  const dLat = radiusM / 111_320;
  const dLon = radiusM / (111_320 * Math.max(Math.cos((lat * Math.PI) / 180), 1e-6));
  const ring: [number, number][] = [];
  for (let i = 0; i <= steps; i++) {
    const a = (i / steps) * 2 * Math.PI;
    ring.push([lon + dLon * Math.cos(a), lat + dLat * Math.sin(a)]);
  }
  return { type: "Polygon", coordinates: [ring] };
}

export type BBox = [number, number, number, number];

export function inBBox([lon, lat]: [number, number], [w, s, e, n]: BBox): boolean {
  return lon >= w && lon <= e && lat >= s && lat <= n;
}

export function normaliseBBox(a: [number, number], b: [number, number]): BBox {
  return [Math.min(a[0], b[0]), Math.min(a[1], b[1]), Math.max(a[0], b[0]), Math.max(a[1], b[1])];
}

export function boundsOf(coords: [number, number][]): BBox | null {
  if (!coords.length) return null;
  let [w, s, e, n] = [Infinity, Infinity, -Infinity, -Infinity];
  for (const [x, y] of coords) {
    w = Math.min(w, x); s = Math.min(s, y); e = Math.max(e, x); n = Math.max(n, y);
  }
  return [w, s, e, n];
}

export function geometryCoords(g: GeoJSON.Polygon | GeoJSON.MultiPolygon | null | undefined): [number, number][] {
  if (!g) return [];
  const polys = g.type === "Polygon" ? [g.coordinates] : g.coordinates;
  return polys.flatMap((p) => p[0].map((c) => [c[0], c[1]] as [number, number]));
}
