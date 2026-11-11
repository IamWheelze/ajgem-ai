import "maplibre-gl/dist/maplibre-gl.css";
/** FR15 map workspace: approved points and boundary, uncertainty, area selection, linked table, source drawer. */
import maplibregl, { type GeoJSONSource, type Map as MLMap, type StyleSpecification } from "maplibre-gl";
import { useEffect, useMemo, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { API_BASE, api, type FeatureCollection, type ProjectDetail, type ToolResult } from "../lib/api";
import { boundsOf, circlePolygon, geometryCoords, inBBox, normaliseBBox, type BBox } from "../lib/geo";
import { useResource } from "../lib/hooks";
import { useScope, useSession } from "../lib/session";
import { useProject } from "../components/Layout";
import { SourceDrawer, evidenceItems, type EvidenceItem } from "../components/SourceEvidence";
import { Empty, ErrorNotice, Loading } from "../components/ui";

const BASEMAP_URL = import.meta.env.VITE_BASEMAP_STYLE_URL as string | undefined;

function cssVar(name: string): string {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim() || "#888";
}

function blankStyle(): StyleSpecification {
  // No third-party tiles by default: basemap rights must be cleared per deployment (FRD §9).
  return { version: 8, sources: {}, layers: [{ id: "bg", type: "background", paint: { "background-color": cssVar("--map-bg") } }] };
}

type Feature = FeatureCollection["features"][number];

interface Georef {
  crs: string; rmse: number | null; max_residual: number | null; points: number; limitations: string;
  blocking: { code: string; message: string }[]; warnings: { code: string; message: string }[];
  residuals: { label: string; residual: number }[];
}
interface Overlay {
  id: string; name: string; status: "valid" | "flagged"; crs: string | null; width: number; height: number;
  control_points: { label: string; col: number; row: number; x: number; y: number }[] | null;
  georef: Georef | null; georef_status: "none" | "draft" | "approved"; georef_by: string | null;
  corners: [number, number][] | null; image_url: string | null; issues: { code: string; message: string }[];
  transform: { pipeline: string; accuracy_m: number | null } | null;
}

export default function MapPage() {
  const { pid } = useProject();
  const nav = useNavigate();
  const { area, setArea } = useScope();
  const fc = useResource(() => api.get<FeatureCollection>(`/projects/${pid}/features`), [pid]);
  const project = useResource(() => api.get<ProjectDetail>(`/projects/${pid}`), [pid]);
  const missing = useResource(() => api.post<{ result: ToolResult }>(`/projects/${pid}/questions`, { tool: "missing_coordinates" }), [pid]);
  const overlays = useResource(() => api.get<{ overlays: Overlay[] }>(`/projects/${pid}/overlays`), [pid]);
  const [shownOverlays, setShownOverlays] = useState<Set<string>>(new Set());
  const box = useRef<HTMLDivElement>(null);
  const map = useRef<MLMap | null>(null);
  const [ready, setReady] = useState(false);
  const [basemapError, setBasemapError] = useState<string | null>(null);
  const [selecting, setSelecting] = useState(false);
  const [drag, setDrag] = useState<{ x0: number; y0: number; x1: number; y1: number } | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const [tableOnly, setTableOnly] = useState(false);
  const [showUncertainty, setShowUncertainty] = useState(true);
  const [drawer, setDrawer] = useState<{ title: string; items: EvidenceItem[] } | null>(null);
  const [showUnmapped, setShowUnmapped] = useState(false);

  const features = fc.data?.features ?? [];
  const visible = useMemo(() => (area ? features.filter((f) => inBBox(f.geometry.coordinates, area.bbox)) : features), [features, area]);

  /* ---- map lifecycle */
  useEffect(() => {
    if (tableOnly || !box.current) return;
    const m = new maplibregl.Map({ container: box.current, style: BASEMAP_URL ?? blankStyle(), center: [7.55, 9.05], zoom: 9, attributionControl: { compact: true } });
    map.current = m;
    m.addControl(new maplibregl.NavigationControl({ showCompass: false }), "top-right");
    m.addControl(new maplibregl.ScaleControl({ unit: "metric" }), "bottom-right");
    m.on("error", (e) => {
      const msg = (e.error as Error | undefined)?.message ?? "Basemap failed to load";
      if (BASEMAP_URL && !m.isStyleLoaded()) {
        setBasemapError(msg);
        m.setStyle(blankStyle());
      }
    });
    m.on("load", () => setReady(true));
    m.on("style.load", () => setReady((r) => r));
    return () => {
      setReady(false);
      m.remove();
      map.current = null;
    };
  }, [tableOnly]);

  /* ---- data layers */
  useEffect(() => {
    const m = map.current;
    if (!m || !ready || !fc.data) return;
    const add = () => {
      const boundary = project.data?.config?.boundary;
      const pts: GeoJSON.FeatureCollection = {
        type: "FeatureCollection",
        features: fc.data!.features.map((f) => ({ type: "Feature", id: f.id, geometry: f.geometry, properties: { id: f.id, sample_id: f.properties.sample_id, censored: f.properties.any_censored } })),
      };
      const unc: GeoJSON.FeatureCollection = {
        type: "FeatureCollection",
        features: fc.data!.features.filter((f) => Number(f.properties.uncertainty_m) > 0).map((f) => ({ type: "Feature", geometry: circlePolygon(f.geometry.coordinates[0], f.geometry.coordinates[1], Number(f.properties.uncertainty_m)), properties: {} })),
      };
      const set = (id: string, data: GeoJSON.GeoJSON) => {
        const s = m.getSource(id) as GeoJSONSource | undefined;
        if (s) s.setData(data);
        else m.addSource(id, { type: "geojson", data, promoteId: id === "samples" ? "id" : undefined });
      };
      set("boundary", boundary ? { type: "Feature", geometry: boundary, properties: {} } : { type: "FeatureCollection", features: [] });
      set("uncertainty", unc);
      set("samples", pts);
      const accent = cssVar("--map-boundary");
      if (!m.getLayer("boundary-fill")) {
        m.addLayer({ id: "boundary-fill", type: "fill", source: "boundary", paint: { "fill-color": accent, "fill-opacity": 0.06 } });
        m.addLayer({ id: "boundary-line", type: "line", source: "boundary", paint: { "line-color": accent, "line-width": 2, "line-dasharray": [3, 2] } });
        m.addLayer({ id: "uncertainty", type: "fill", source: "uncertainty", paint: { "fill-color": cssVar("--map-point"), "fill-opacity": 0.12 } });
        m.addLayer({
          id: "samples", type: "circle", source: "samples",
          paint: {
            "circle-radius": ["case", ["boolean", ["feature-state", "selected"], false], 8, 5],
            "circle-color": ["case", ["get", "censored"], "rgba(0,0,0,0)", cssVar("--map-point")],
            "circle-stroke-color": ["case", ["boolean", ["feature-state", "selected"], false], cssVar("--text"), ["case", ["get", "censored"], cssVar("--map-point-censored"), cssVar("--surface")]],
            "circle-stroke-width": ["case", ["get", "censored"], 2.5, ["boolean", ["feature-state", "selected"], false], 2.5, 1],
          },
        });
        m.on("click", "samples", (e) => {
          const id = e.features?.[0]?.properties?.id as string | undefined;
          if (id) setSelected(id);
        });
        m.on("mouseenter", "samples", () => (m.getCanvas().style.cursor = "pointer"));
        m.on("mouseleave", "samples", () => (m.getCanvas().style.cursor = ""));
      }
      const b = boundsOf([...geometryCoords(boundary), ...fc.data!.features.map((f) => f.geometry.coordinates)]);
      if (b) m.fitBounds([[b[0], b[1]], [b[2], b[3]]], { padding: 48, duration: 0, maxZoom: 14 });
    };
    if (m.isStyleLoaded()) add();
    else m.once("idle", add);
  }, [ready, fc.data, project.data]);

  useEffect(() => {
    const m = map.current;
    if (m && ready && m.getLayer("uncertainty")) m.setLayoutProperty("uncertainty", "visibility", showUncertainty ? "visible" : "none");
  }, [showUncertainty, ready]);

  /* ---- selected area outline */
  useEffect(() => {
    const m = map.current;
    if (!m || !ready || !m.getLayer("samples")) return;
    const data: GeoJSON.FeatureCollection = area
      ? { type: "FeatureCollection", features: [{ type: "Feature", properties: {}, geometry: { type: "Polygon", coordinates: [[[area.bbox[0], area.bbox[1]], [area.bbox[2], area.bbox[1]], [area.bbox[2], area.bbox[3]], [area.bbox[0], area.bbox[3]], [area.bbox[0], area.bbox[1]]]] } }] }
      : { type: "FeatureCollection", features: [] };
    const src = m.getSource("area") as GeoJSONSource | undefined;
    if (src) src.setData(data);
    else {
      m.addSource("area", { type: "geojson", data });
      m.addLayer({ id: "area", type: "line", source: "area", paint: { "line-color": cssVar("--accent"), "line-width": 2 } }, "samples");
    }
  }, [area, ready, fc.data]);

  /* ---- raster overlays (FR23): only validated ones, at their transformed corners, beneath the samples */
  useEffect(() => {
    const m = map.current;
    if (!m || !ready) return;
    for (const o of overlays.data?.overlays ?? []) {
      const id = `overlay-${o.id}`;
      const on = shownOverlays.has(o.id) && o.status === "valid" && o.corners && o.image_url;
      if (on && !m.getSource(id)) {
        m.addSource(id, { type: "image", url: `${API_BASE}${o.image_url}`, coordinates: o.corners as [[number, number], [number, number], [number, number], [number, number]] });
        m.addLayer({ id, type: "raster", source: id, paint: { "raster-opacity": 0.75 } }, m.getLayer("boundary-fill") ? "boundary-fill" : undefined);
      } else if (!on && m.getSource(id)) {
        m.removeLayer(id);
        m.removeSource(id);
      }
    }
  }, [overlays.data, shownOverlays, ready]);

  /* ---- selection sync (map ↔ table) */
  const prevSel = useRef<string | null>(null);
  useEffect(() => {
    const m = map.current;
    if (!m || !ready || !m.getSource("samples")) return;
    if (prevSel.current) m.setFeatureState({ source: "samples", id: prevSel.current }, { selected: false });
    if (selected) {
      m.setFeatureState({ source: "samples", id: selected }, { selected: true });
      const f = features.find((x) => x.id === selected);
      if (f && !m.getBounds().contains(f.geometry.coordinates)) m.easeTo({ center: f.geometry.coordinates });
      document.getElementById(`row-${selected}`)?.scrollIntoView({ block: "nearest" });
    }
    prevSel.current = selected;
  }, [selected, ready, features]);

  /* ---- rectangle area selection */
  useEffect(() => {
    const m = map.current;
    if (!m) return;
    if (selecting) { m.dragPan.disable(); m.boxZoom.disable(); } else { m.dragPan.enable(); m.boxZoom.enable(); }
  }, [selecting]);
  const onDown = (e: React.MouseEvent) => {
    if (!selecting) return;
    const r = box.current!.getBoundingClientRect();
    setDrag({ x0: e.clientX - r.left, y0: e.clientY - r.top, x1: e.clientX - r.left, y1: e.clientY - r.top });
  };
  const onMove = (e: React.MouseEvent) => {
    if (!drag) return;
    const r = box.current!.getBoundingClientRect();
    setDrag({ ...drag, x1: e.clientX - r.left, y1: e.clientY - r.top });
  };
  const onUp = () => {
    const m = map.current;
    if (!drag || !m) return;
    const a = m.unproject([drag.x0, drag.y0]);
    const b = m.unproject([drag.x1, drag.y1]);
    const bbox: BBox = normaliseBBox([a.lng, a.lat], [b.lng, b.lat]);
    const inside = features.filter((f) => inBBox(f.geometry.coordinates, bbox));
    setArea({ bbox, sampleKeys: inside.map((f) => f.properties.sample_key), sampleIds: inside.map((f) => f.properties.sample_id) });
    setDrag(null);
    setSelecting(false);
  };

  const sel = features.find((f) => f.id === selected) ?? null;
  const openEvidence = (f: Feature) => setDrawer({ title: f.properties.sample_id, items: evidenceItems(undefined, f.properties.anchor_ids) });

  return (
    <div className="page full">
      <div className="mapws" style={tableOnly ? { gridTemplateColumns: "1fr" } : undefined}>
        {!tableOnly && (
          <div className={`mapbox ${selecting ? "selecting" : ""}`} onMouseDown={onDown} onMouseMove={onMove} onMouseUp={onUp}>
            <div ref={box} style={{ position: "absolute", inset: 0 }} aria-label="Project map" role="region" />
            {drag && <div className="select-box" style={{ left: Math.min(drag.x0, drag.x1), top: Math.min(drag.y0, drag.y1), width: Math.abs(drag.x1 - drag.x0), height: Math.abs(drag.y1 - drag.y0) }} />}
            <div className="map-tools">
              <button className={selecting ? "primary" : ""} onClick={() => setSelecting((s) => !s)} aria-pressed={selecting}>{selecting ? "Drag a rectangle…" : "Select area"}</button>
              {area && <button onClick={() => setArea(null)}>Clear area ({area.sampleIds.length})</button>}
              {area && <button className="primary" onClick={() => nav(`/p/${pid}/assistant`)}>Ask about this area</button>}
              <button onClick={() => setShowUncertainty((s) => !s)} aria-pressed={showUncertainty}>{showUncertainty ? "Hide" : "Show"} uncertainty</button>
              <button onClick={() => setTableOnly(true)}>Table only</button>
            </div>
            <div className="card map-legend">
              <div><span className="legend-dot" style={{ background: "var(--map-point)", border: "1px solid var(--surface)" }} />Approved sample, exact results</div>
              <div><span className="legend-dot" style={{ border: "2.5px solid var(--map-point-censored)" }} />Has a censored result (&lt; or &gt;)</div>
              <div><span className="legend-dot" style={{ background: "color-mix(in srgb, var(--map-point) 25%, transparent)" }} />Positional uncertainty (not a mineralisation contour)</div>
              <div><span className="legend-dot" style={{ border: "2px dashed var(--map-boundary)", borderRadius: 2 }} />Project boundary (administrative context)</div>
              {!BASEMAP_URL && <div className="faint">No basemap configured (set VITE_BASEMAP_STYLE_URL once tile rights are cleared).</div>}
            </div>
          </div>
        )}
        <aside className="map-side" aria-label="Linked table">
          <div className="card-pad stack" style={{ borderBottom: "1px solid var(--border)" }}>
            <div className="row between">
              <h2>{area ? `Selected area · ${visible.length}` : `Mapped samples · ${features.length}`}</h2>
              {tableOnly && <button className="small" onClick={() => setTableOnly(false)}>Show map</button>}
            </div>
            {!!overlays.data?.overlays.length && (
              <fieldset className="stack" style={{ border: 0, padding: 0, margin: 0 }}>
                <legend className="faint">Raster overlays</legend>
                {overlays.data.overlays.map((o) => o.status === "valid" ? (
                  <label key={o.id} className="check" title={`${o.crs} · ${o.width}×${o.height} px · ${o.transform?.pipeline ?? ""}`}>
                    <input type="checkbox" checked={shownOverlays.has(o.id)} disabled={tableOnly} onChange={(e) => {
                      const n = new Set(shownOverlays); if (e.target.checked) n.add(o.id); else n.delete(o.id); setShownOverlays(n);
                    }} /> {o.name} <span className="faint">{o.crs}{o.transform?.accuracy_m ? ` · ±${o.transform.accuracy_m} m transform` : ""}</span>
                  </label>
                ) : (
                  <div key={o.id} className="notice warn"><div><strong>{o.name} is not shown</strong>{o.issues.map((i) => i.message).join(" ")}
                    <GeorefPanel o={o} onChange={overlays.reload} /></div></div>
                ))}
              </fieldset>
            )}
            {basemapError && <div className="notice warn"><div><strong>Basemap unavailable</strong>{basemapError}. Project data is still shown.</div></div>}
            {fc.data && fc.data.unmapped_count > 0 && (
              <div className="notice info">
                <div>
                  <strong>{fc.data.unmapped_count} approved sample(s) are not on the map</strong>
                  Unknown or unconfirmed CRS, or no coordinates. They are never placed at (0,0). <button className="link" onClick={() => setShowUnmapped((s) => !s)}>{showUnmapped ? "Hide" : "List them"}</button>
                  {showUnmapped && missing.data && (
                    <table className="data" style={{ marginTop: 6 }}>
                      <tbody>{missing.data.result.rows.map((r) => <tr key={r.record_id as string}><td className="mono">{r.sample_id}</td><td><code>{String(r.reason)}</code></td></tr>)}</tbody>
                    </table>
                  )}
                </div>
              </div>
            )}
            {sel && (
              <div className="card card-pad stack">
                <div className="row between"><strong className="mono">{sel.properties.sample_id}</strong><button className="small" onClick={() => openEvidence(sel)}>Source evidence</button></div>
                <div className="faint mono">{sel.geometry.coordinates.map((c) => c.toFixed(6)).join(", ")} · ±{sel.properties.uncertainty_m ?? "?"} m · {String(sel.properties.transform?.source_crs ?? "")}</div>
                <table className="data">
                  <tbody>{sel.properties.assays.map((a) => <tr key={a.record_id}><td>{a.analyte}</td><td className="num">{a.result_raw} {a.unit}</td><td>{a.censored && <span className="badge indet">censored</span>}</td></tr>)}</tbody>
                </table>
                {sel.properties.transform && (
                  <details><summary>Transformation pipeline</summary><pre className="json">{String(sel.properties.transform.pipeline)}</pre></details>
                )}
              </div>
            )}
          </div>
          <ErrorNotice error={fc.error} />
          {fc.loading && !fc.data ? <Loading /> : visible.length === 0 ? <Empty>No approved, mapped samples{area ? " in this area" : ""}.</Empty> : (
            <div className="table-wrap" tabIndex={0}>
              <table className="data">
                <thead><tr><th>Sample</th><th>Results</th><th className="num">± m</th></tr></thead>
                <tbody>
                  {visible.map((f) => (
                    <tr key={f.id} id={`row-${f.id}`} className={`clickable ${f.id === selected ? "selected" : ""}`} onClick={() => setSelected(f.id)}>
                      <td className="mono">{f.properties.sample_id}</td>
                      <td>{f.properties.assays.map((a) => <span key={a.record_id} className="mono" style={{ marginRight: 8, color: a.censored ? "var(--indet)" : undefined }}>{a.analyte} {a.result_raw}</span>)}</td>
                      <td className="num">{f.properties.uncertainty_m ?? "—"}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </aside>
      </div>
      {drawer && <SourceDrawer title={`Evidence · ${drawer.title}`} items={drawer.items} onClose={() => setDrawer(null)} />}
    </div>
  );
}


/** FR26: control points for a scanned map, the fit's residuals, and reviewer approval (a second person). */
function GeorefPanel({ o, onChange }: { o: Overlay; onChange: () => void }) {
  const { roles } = useProject();
  const { me } = useSession();
  const [open, setOpen] = useState(false);
  const [crs, setCrs] = useState(o.georef?.crs ?? "EPSG:32632");
  const [text, setText] = useState((o.control_points ?? []).map((p) => `${p.label},${p.col},${p.row},${p.x},${p.y}`).join("\n"));
  const [err, setErr] = useState<unknown>();
  const canEdit = roles.has("analyst") || roles.has("reviewer");
  const g = o.georef;
  const save = async () => {
    setErr(undefined);
    try {
      const points = text.split("\n").map((l) => l.trim()).filter(Boolean).map((l) => {
        const [label, col, row, x, y] = l.split(",").map((v) => v.trim());
        return { label, col: Number(col), row: Number(row), x: Number(x), y: Number(y) };
      });
      await api.put(`/overlays/${o.id}/control-points`, { crs, points });
      onChange();
    } catch (e) { setErr(e); }
  };
  const approve = async () => { setErr(undefined); try { await api.post(`/overlays/${o.id}/approve-georeference`); onChange(); } catch (e) { setErr(e); } };
  if (!canEdit && !g) return null;
  return (
    <div className="stack" style={{ marginTop: 6 }}>
      <button type="button" className="link" onClick={() => setOpen((v) => !v)} aria-expanded={open}>{open ? "Hide georeferencing" : "Georeference from control points…"}</button>
      {open && (
        <div className="stack">
          {o.image_url && <a href={`${API_BASE}${o.image_url}`} target="_blank" rel="noreferrer">Open the scan ({o.width}×{o.height} px) to read pixel positions ↗</a>}
          <div className="faint">Enter at least 4 points spread towards the corners, one per line: <code>label, column, row, x, y</code>, with x/y read from a stated map source, never from OCR'd place names.</div>
          {canEdit && <>
            <label className="field">CRS of the map coordinates<input value={crs} onChange={(e) => setCrs(e.target.value)} /></label>
            <label className="field">Control points<textarea rows={5} value={text} onChange={(e) => setText(e.target.value)} placeholder={"P1, 50, 40, 335500, 995600\nP2, 950, 60, 344500, 995400"} /></label>
            <div><button className="small" onClick={save}>Fit</button></div>
          </>}
          {g && (
            <div className="stack">
              <div>{g.points} points · RMSE {g.rmse != null ? g.rmse.toFixed(2) : "—"} · max residual {g.max_residual != null ? g.max_residual.toFixed(2) : "—"} ({g.crs} units) · <StatusLabel s={o.georef_status} /></div>
              {g.blocking.map((b) => <div key={b.code} className="faint"><code>{b.code}</code> {b.message}</div>)}
              {g.warnings.map((w) => <div key={w.message} className="faint"><code>{w.code}</code> {w.message}</div>)}
              <div className="faint">{g.limitations}</div>
              {roles.has("reviewer") && o.georef_status === "draft" && !g.blocking.length && o.georef_by !== me?.id && (
                <div><button className="small primary" onClick={approve}>Approve georeference</button></div>
              )}
            </div>
          )}
          <ErrorNotice error={err} />
        </div>
      )}
    </div>
  );
}

function StatusLabel({ s }: { s: Overlay["georef_status"] }) {
  return <span className={`badge ${s === "approved" ? "ok" : s === "draft" ? "warn" : ""}`}>{s === "draft" ? "awaiting review" : s}</span>;
}
