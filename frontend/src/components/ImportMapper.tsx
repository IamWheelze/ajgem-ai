/** FR07 guided mapping: choose sheet/header/delimiter, map columns and units, confirm CRS, preview, commit. */
import { useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { api, type Issue } from "../lib/api";
import { useAction, useResource } from "../lib/hooks";
import { useProject } from "./Layout";
import { ErrorNotice, Loading } from "./ui";

interface TableHead {
  format: string;
  header: string[];
  meta: { sheets?: string[]; sheet?: string; delimiter?: string; encoding?: string };
  row_count: number;
  sample_rows: { row: number; cells: Record<string, string | null> }[];
  targets: string[];
}
interface Preview {
  id: string;
  preview_hash: string;
  report: { counts: Record<string, number>; rows: { row: number; sample_id: string | null; issues: Issue[] }[]; rows_truncated: boolean; versions: Record<string, string> };
}
interface AnalyteMap { column: string; analyte: string; unit: string; basis?: string }

const TARGET_LABEL: Record<string, string> = {
  sample_id: "Sample ID *", campaign: "Campaign", x: "X / Easting / Longitude", y: "Y / Northing / Latitude",
  certificate_number: "Certificate number", laboratory: "Laboratory", method: "Method", basis: "Basis",
  detection_limit: "Detection limit", detection_limit_unit: "DL unit", sample_type: "Sample type",
  collection_date: "Collection date", uncertainty_m: "Uncertainty (m)",
};
const GUESS: [RegExp, string][] = [
  [/^(sample[\s_-]?id|sample|sampleno|sample[\s_-]?no)$/i, "sample_id"], [/campaign|programme|program/i, "campaign"],
  [/^(x|easting|east|lon|longitude)$/i, "x"], [/^(y|northing|north|lat|latitude)$/i, "y"],
  [/cert/i, "certificate_number"], [/^lab/i, "laboratory"], [/method/i, "method"], [/basis/i, "basis"],
  [/^(dl|detection)/i, "detection_limit"], [/type/i, "sample_type"], [/date/i, "collection_date"],
];
const ANALYTE_COL = /^([A-Z][a-z]?\d?(?:O\d?)?\d?)[\s_-]*(?:\(?\s*(ppm|ppb|%|pct|g\/t)\s*\)?)?$/;

export function guessColumns(header: string[]) {
  const cols: Record<string, string> = {};
  const analytes: AnalyteMap[] = [];
  for (const h of header) {
    const g = GUESS.find(([rx]) => rx.test(h.trim()));
    if (g && !cols[g[1]]) { cols[g[1]] = h; continue; }
    const m = h.trim().match(ANALYTE_COL);
    if (m && m[2]) analytes.push({ column: h, analyte: m[1], unit: m[2] === "pct" ? "%" : m[2] });
  }
  return { cols, analytes };
}

export function ImportMapper({ versionId, format }: { versionId: string; format: string }) {
  const { pid, reload } = useProject();
  const [sheet, setSheet] = useState<string>();
  const [headerRow, setHeaderRow] = useState(1);
  const [delimiter, setDelimiter] = useState("");
  const head = useResource(() => api.get<TableHead>(`/versions/${versionId}/table`, { sheet, header_row: headerRow, delimiter }), [versionId, sheet, headerRow, delimiter]);
  const guessed = useMemo(() => (head.data ? guessColumns(head.data.header) : { cols: {}, analytes: [] }), [head.data]);
  const [cols, setCols] = useState<Record<string, string> | null>(null);
  const [analytes, setAnalytes] = useState<AnalyteMap[] | null>(null);
  const [crs, setCrs] = useState({ source_crs: "", axis_order: "xy", confirmed: false, uncertainty_m: "", positioning_method: "" });
  const [consts, setConsts] = useState({ campaign: "", laboratory: "", certificate_number: "", method: "", basis: "" });
  const [preview, setPreview] = useState<Preview | null>(null);
  const [committed, setCommitted] = useState<{ created: Record<string, number>; already_present: number; linked_existing_samples: unknown[] } | null>(null);
  const C = cols ?? guessed.cols;
  const A = analytes ?? guessed.analytes;

  const mapping = () => {
    const m: Record<string, unknown> = { header_row: headerRow, columns: Object.fromEntries(Object.entries(C).filter(([, v]) => v)), analytes: A.filter((a) => a.column && a.analyte && a.unit) };
    if (format === "xlsx" && (sheet ?? head.data?.meta.sheet)) m.sheet = sheet ?? head.data?.meta.sheet;
    if (format === "csv") m.delimiter = delimiter || head.data?.meta.delimiter;
    const k = Object.fromEntries(Object.entries(consts).filter(([f, v]) => v && !C[f]));
    if (Object.keys(k).length) m.constants = k;
    if (crs.source_crs.trim()) m.crs = { ...crs, source_crs: crs.source_crs.trim(), uncertainty_m: crs.uncertainty_m || undefined, positioning_method: crs.positioning_method || undefined };
    return m;
  };
  const doPreview = useAction(async () => { setCommitted(null); setPreview(await api.post<Preview>(`/versions/${versionId}/import-previews`, { mapping: mapping() })); });
  const doCommit = useAction(async () => { setCommitted(await api.post(`/imports/${preview!.id}/commit`, { preview_hash: preview!.preview_hash })); reload(); });

  if (head.loading && !head.data) return <Loading what="Reading table" />;
  if (head.error) return <ErrorNotice error={head.error} />;
  const h = head.data!;
  const setCol = (t: string, v: string) => { setCols({ ...C, [t]: v }); setPreview(null); };
  const setA = (i: number, patch: Partial<AnalyteMap>) => { setAnalytes(A.map((a, n) => (n === i ? { ...a, ...patch } : a))); setPreview(null); };
  const hasCoords = Boolean(C.x && C.y);

  return (
    <div className="card">
      <div className="card-head"><h2>Map columns</h2><span className="faint">{h.row_count} data rows · {h.header.length} columns</span></div>
      <div className="card-pad stack">
        <div className="form-grid">
          {format === "xlsx" && (
            <label className="field">Sheet
              <select value={sheet ?? h.meta.sheet} onChange={(e) => { setSheet(e.target.value); setCols(null); setAnalytes(null); }}>
                {h.meta.sheets?.map((s) => <option key={s}>{s}</option>)}
              </select>
            </label>
          )}
          <label className="field">Header row<input type="number" min={1} value={headerRow} onChange={(e) => { setHeaderRow(Math.max(1, Number(e.target.value))); setCols(null); setAnalytes(null); }} /></label>
          {format === "csv" && (
            <label className="field">Delimiter
              <select value={delimiter || h.meta.delimiter} onChange={(e) => setDelimiter(e.target.value)}>
                <option value=",">Comma ,</option><option value=";">Semicolon ;</option><option value={"\t"}>Tab</option><option value="|">Pipe |</option>
              </select>
            </label>
          )}
        </div>

        <details open>
          <summary>First rows (raw values, as stored)</summary>
          <div className="table-wrap" style={{ maxHeight: 240, marginTop: 8 }}>
            <table className="data">
              <thead><tr><th className="num">Row</th>{h.header.map((c) => <th key={c}>{c}</th>)}</tr></thead>
              <tbody>{h.sample_rows.map((r) => <tr key={r.row}><td className="num">{r.row}</td>{h.header.map((c) => <td key={c} className="mono">{r.cells[c] ?? ""}</td>)}</tr>)}</tbody>
            </table>
          </div>
        </details>

        <h3>Fields</h3>
        <div className="form-grid">
          {Object.keys(TARGET_LABEL).map((t) => (
            <label key={t} className="field">{TARGET_LABEL[t]}
              <select value={C[t] ?? ""} onChange={(e) => setCol(t, e.target.value)}>
                <option value="">— not in file —</option>
                {h.header.map((c) => <option key={c} value={c}>{c}</option>)}
              </select>
            </label>
          ))}
        </div>
        <details>
          <summary>Constant values for fields not in the file</summary>
          <div className="form-grid" style={{ marginTop: 8 }}>
            {(Object.keys(consts) as (keyof typeof consts)[]).map((k) => (
              <label key={k} className="field">{TARGET_LABEL[k] ?? k}
                <input value={consts[k]} disabled={Boolean(C[k])} onChange={(e) => setConsts({ ...consts, [k]: e.target.value })} />
              </label>
            ))}
          </div>
        </details>

        <h3>Analyte columns</h3>
        <table className="data">
          <thead><tr><th>Column</th><th>Analyte (as reported)</th><th>Unit</th><th>Basis</th><th /></tr></thead>
          <tbody>
            {A.map((a, i) => (
              <tr key={i}>
                <td><select value={a.column} onChange={(e) => setA(i, { column: e.target.value })}><option value="">—</option>{h.header.map((c) => <option key={c}>{c}</option>)}</select></td>
                <td><input value={a.analyte} onChange={(e) => setA(i, { analyte: e.target.value })} placeholder="Li, Li2O, Ta2O5…" aria-label="Analyte" /></td>
                <td><input value={a.unit} onChange={(e) => setA(i, { unit: e.target.value })} placeholder="ppm" aria-label="Unit" /></td>
                <td><select value={a.basis ?? ""} onChange={(e) => setA(i, { basis: e.target.value || undefined })}><option value="">from Basis field</option><option value="dry">dry</option><option value="as_received">as received</option></select></td>
                <td><button className="small ghost" onClick={() => { setAnalytes(A.filter((_, n) => n !== i)); setPreview(null); }} aria-label="Remove analyte">✕</button></td>
              </tr>
            ))}
          </tbody>
        </table>
        <div><button className="small" onClick={() => setAnalytes([...A, { column: "", analyte: "", unit: "" }])}>Add analyte column</button></div>
        <p className="faint">Elemental and oxide analytes are kept distinct: map the analyte exactly as the laboratory reported it. Units are never guessed during review — an unknown or ambiguous unit (e.g. “ppt”) blocks approval.</p>

        {hasCoords && (
          <>
            <h3>Coordinate reference</h3>
            <div className="form-grid">
              <label className="field">Source CRS (EPSG code or definition)
                <input value={crs.source_crs} onChange={(e) => { setCrs({ ...crs, source_crs: e.target.value, confirmed: false }); setPreview(null); }} placeholder="e.g. EPSG:32632 or EPSG:26332" />
              </label>
              <fieldset className="field" style={{ border: 0, padding: 0, margin: 0 }}>
                <legend style={{ fontSize: 12.5 }}>Axis order of the columns</legend>
                <label className="check"><input type="radio" checked={crs.axis_order === "xy"} onChange={() => setCrs({ ...crs, axis_order: "xy" })} /> X/Easting/Longitude in the X column</label>
                <label className="check"><input type="radio" checked={crs.axis_order === "yx"} onChange={() => setCrs({ ...crs, axis_order: "yx" })} /> Swapped (Y first)</label>
              </fieldset>
              <label className="field">Positional uncertainty (m)<input value={crs.uncertainty_m} onChange={(e) => setCrs({ ...crs, uncertainty_m: e.target.value })} placeholder="e.g. 10" /></label>
              <label className="field">Positioning method<input value={crs.positioning_method} onChange={(e) => setCrs({ ...crs, positioning_method: e.target.value })} placeholder="handheld GPS, DGPS…" /></label>
            </div>
            <label className="check">
              <input type="checkbox" disabled={!crs.source_crs.trim()} checked={crs.confirmed} onChange={(e) => { setCrs({ ...crs, confirmed: e.target.checked }); setPreview(null); }} />
              I have confirmed this CRS and axis order against source evidence (survey metadata, report or field notes).
            </label>
            {!crs.source_crs.trim() && <div className="notice warn"><div>No CRS: samples will be imported but stay <strong>unmapped</strong>. The project's working CRS is never assigned to input data.</div></div>}
          </>
        )}

        <div className="row">
          <button className="primary" disabled={!C.sample_id || doPreview.pending} onClick={doPreview.run}>{doPreview.pending ? "Validating…" : "Preview & validate"}</button>
          {!C.sample_id && <span className="faint">Map a Sample ID column to continue.</span>}
        </div>
        <ErrorNotice error={doPreview.error} />
        {preview && <PreviewReport p={preview} />}
        {preview && !committed && (
          <div className="row">
            <button className="primary" disabled={doCommit.pending} onClick={doCommit.run}>Create {preview.report.counts.samples + preview.report.counts.assays} candidates for review</button>
            <span className="faint">Commit is bound to preview {preview.preview_hash.slice(0, 10)}…; re-running the same import creates no duplicates.</span>
          </div>
        )}
        <ErrorNotice error={doCommit.error} />
        {committed && (
          <div className="notice ok">
            <div>
              <strong>Imported as candidates</strong>
              {committed.created.sample ?? 0} samples, {committed.created.assay ?? 0} assays, {committed.created.certificate ?? 0} certificates created
              {committed.already_present ? `; ${committed.already_present} already present` : ""}
              {committed.linked_existing_samples.length ? `; ${committed.linked_existing_samples.length} rows referred to samples already in the project and were not duplicated` : ""}.
              {" "}<Link to={`/p/${pid}/review`}>Go to review</Link>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

function PreviewReport({ p }: { p: Preview }) {
  const c = p.report.counts;
  const withIssues = p.report.rows.filter((r) => r.issues.some((i) => i.severity !== "info"));
  return (
    <div className="stack">
      <div className="tiles">
        <div className="card tile"><span className="label">Rows</span><span className="value">{c.rows}</span></div>
        <div className="card tile"><span className="label">Samples / assays</span><span className="value">{c.samples} / {c.assays}</span></div>
        <div className="card tile"><span className="label">Critical issues</span><span className="value" style={{ color: c.critical ? "var(--bad)" : undefined }}>{c.critical ?? 0}</span><span className="sub">block approval until corrected</span></div>
        <div className="card tile"><span className="label">Warnings</span><span className="value" style={{ color: c.warning ? "var(--warn)" : undefined }}>{c.warning ?? 0}</span><span className="sub">need reviewer acknowledgement</span></div>
      </div>
      {withIssues.length > 0 && (
        <div className="table-wrap" style={{ maxHeight: 300 }}>
          <table className="data">
            <thead><tr><th className="num">Row</th><th>Sample</th><th>Issues</th></tr></thead>
            <tbody>
              {withIssues.map((r) => (
                <tr key={r.row}>
                  <td className="num">{r.row}</td>
                  <td className="mono">{r.sample_id ?? "—"}</td>
                  <td>{r.issues.filter((i) => i.severity !== "info").map((i, n) => (
                    <div key={n}><span className={`badge ${i.severity === "critical" ? "bad" : "warn"}`}>{i.severity}</span> <code>{i.code}</code> <span className="faint">{i.message}</span></div>
                  ))}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {p.report.rows_truncated && <p className="faint">Showing the first 500 rows of the report.</p>}
    </div>
  );
}
