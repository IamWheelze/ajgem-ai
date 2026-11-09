/** Jobs and exports: processing state, retry/cancel, export with provenance manifest (FR05, FR20). */
import { useState } from "react";
import { API_BASE, api, authHeaders, type JobView } from "../lib/api";
import { when } from "../lib/format";
import { RUNNING_JOB_STATES, useAction, usePolling, useResource } from "../lib/hooks";
import { useProject } from "../components/Layout";
import { Empty, ErrorNotice, StatusBadge } from "../components/ui";
import { JobPanel } from "./Upload";

interface ExportRow { id: string; format: string; purpose: string; created_at: string; manifest: Record<string, unknown> & { record_count?: number; unmapped_count?: number; coordinates_withheld?: boolean; unresolved?: Record<string, number>; data_sha256?: string; revoked?: { message: string; deletion_request_id: string } } }

export default function Jobs() {
  const { pid, can } = useProject();
  const jobs = useResource(() => api.get<{ jobs: JobView[] }>(`/projects/${pid}/jobs`), [pid]);
  usePolling((jobs.data?.jobs ?? []).some((j) => RUNNING_JOB_STATES.includes(j.state)), jobs.reload, 3000);
  const [open, setOpen] = useState<string | null>(null);
  return (
    <div className="page">
      <div className="page-head"><div><h1>Jobs & exports</h1><p>Processing runs per page; failed pages retry individually without duplicating successful candidates.</p></div></div>
      <section className="card">
        <div className="card-head"><h2>Processing jobs</h2><button className="small" onClick={jobs.reload}>Refresh</button></div>
        {!jobs.data?.jobs.length ? <Empty>No jobs.</Empty> : (
          <table className="data">
            <thead><tr><th>Job</th><th>State</th><th className="num">Pages</th><th className="num">Attempt</th><th>Failed units</th><th>Started</th><th /></tr></thead>
            <tbody>
              {jobs.data.jobs.map((j) => (
                <tr key={j.id} className={`clickable ${open === j.id ? "selected" : ""}`} onClick={() => setOpen(open === j.id ? null : j.id)}>
                  <td>{j.kind.replace("_", " ")} <span className="faint mono">{j.id.slice(0, 8)}</span></td>
                  <td><StatusBadge status={j.state} /></td>
                  <td className="num">{j.cost_counters.pages ?? "—"}</td>
                  <td className="num">{j.attempt}</td>
                  <td>{j.retryable.length || "—"}</td>
                  <td className="faint">{when(j.created_at)}</td>
                  <td>{["queued", "processing", "partial_failed", "awaiting_budget_approval"].includes(j.state) && can("upload") && <button className="small" onClick={async (e) => { e.stopPropagation(); await api.post(`/jobs/${j.id}/cancel`); jobs.reload(); }}>Cancel</button>}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>
      {open && <JobPanel key={open} jobId={open} />}
      <Exports />
    </div>
  );
}

function Exports() {
  const { pid, can } = useProject();
  const list = useResource(() => api.get<{ exports: ExportRow[] }>(`/projects/${pid}/exports`), [pid]);
  const [fmt, setFmt] = useState<"csv" | "geojson" | "gpkg">("csv");
  const [analyte, setAnalyte] = useState("");
  const [purpose, setPurpose] = useState("");
  const [view, setView] = useState<ExportRow | null>(null);
  const create = useAction(async () => {
    const r = await api.post<{ id: string; manifest: ExportRow["manifest"] }>(`/projects/${pid}/exports`, { format: fmt, purpose, filters: analyte ? { analyte } : {} });
    list.reload();
    setView({ id: r.id, format: fmt, purpose, created_at: new Date().toISOString(), manifest: r.manifest });
  });
  const download = useAction(async (id: string) => {
    // Authorisation is re-checked by the server at download time.
    const res = await fetch(`${API_BASE}/exports/${id}/download`, { headers: authHeaders() });
    if (!res.ok) throw new Error((await res.json().catch(() => null))?.error?.message ?? res.statusText);
    const url = URL.createObjectURL(await res.blob());
    const a = document.createElement("a");
    a.href = url;
    a.download = `export-${id}.zip`;
    a.click();
    URL.revokeObjectURL(url);
  });
  if (!can("export")) return <section className="card card-pad"><h2>Exports</h2><p className="muted">Exporting needs a separate export grant from a project administrator.</p></section>;
  return (
    <section className="card">
      <div className="card-head"><h2>Exports</h2><span className="faint">Approved records only · ZIP with data and provenance manifest</span></div>
      <form className="card-pad row" onSubmit={(e) => { e.preventDefault(); create.run(); }} style={{ alignItems: "flex-end" }}>
        <label className="field">Format<select value={fmt} onChange={(e) => setFmt(e.target.value as "csv" | "geojson" | "gpkg")}><option value="csv">CSV</option><option value="geojson">GeoJSON</option><option value="gpkg">GeoPackage</option></select></label>
        <label className="field">Analyte<input value={analyte} onChange={(e) => setAnalyte(e.target.value)} placeholder="all" style={{ width: 90 }} /></label>
        <label className="field" style={{ flex: 1 }}>Purpose (recorded)<input value={purpose} onChange={(e) => setPurpose(e.target.value)} placeholder="e.g. Quarterly technical review pack" /></label>
        <button className="primary" disabled={!purpose.trim() || create.pending}>Create export</button>
      </form>
      <div className="card-pad" style={{ paddingTop: 0 }}><ErrorNotice error={create.error ?? download.error} /></div>
      {view && <Manifest e={view} onDownload={() => download.run(view.id)} />}
      {!list.data?.exports.length ? <Empty>No exports yet.</Empty> : (
        <table className="data">
          <thead><tr><th>Created</th><th>Format</th><th>Purpose</th><th className="num">Records</th><th className="num">Unmapped</th><th>Unresolved</th><th /></tr></thead>
          <tbody>
            {list.data.exports.map((e) => (
              <tr key={e.id} className="clickable" onClick={() => setView(e)}>
                <td className="faint">{when(e.created_at)}</td>
                <td>{e.format}</td>
                <td>{e.purpose}</td>
                <td className="num">{e.manifest.record_count ?? "—"}</td>
                <td className="num">{e.manifest.unmapped_count ?? "—"}</td>
                <td>{e.manifest.revoked ? <span className="badge bad">withdrawn — evidence deleted</span> : e.manifest.unresolved?.open_conflicts ? <span className="badge bad">{e.manifest.unresolved.open_conflicts} open conflicts</span> : <span className="faint">none</span>}</td>
                <td>{!e.manifest.revoked && <button className="small" onClick={(ev) => { ev.stopPropagation(); download.run(e.id); }}>Download</button>}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  );
}

function Manifest({ e, onDownload }: { e: ExportRow; onDownload: () => void }) {
  const m = e.manifest;
  if (m.revoked)
    return <div className="card-pad"><div className="notice bad"><div><strong>Export withdrawn</strong>{m.revoked.message} Its file has been erased.</div></div></div>;
  return (
    <div className="card-pad stack" style={{ borderTop: "1px solid var(--border)", borderBottom: "1px solid var(--border)", background: "var(--surface-2)" }}>
      <div className="row between"><strong>Manifest · {e.purpose}</strong><button className="primary small" onClick={onDownload}>Download ZIP</button></div>
      <div className="row">
        <span className="badge">{m.record_count} records</span>
        <span className="badge">{m.unmapped_count} unmapped</span>
        {m.coordinates_withheld && <span className="badge warn">exact coordinates withheld</span>}
        {Object.entries(m.unresolved ?? {}).map(([k, v]) => <span key={k} className={`badge ${v ? "warn" : ""}`}>{k.replaceAll("_", " ")}: {v}</span>)}
        <span className="faint mono">data sha256 {m.data_sha256?.slice(0, 16)}…</span>
      </div>
      <details><summary>Full manifest</summary><pre className="json">{JSON.stringify(m, null, 2)}</pre></details>
    </div>
  );
}
