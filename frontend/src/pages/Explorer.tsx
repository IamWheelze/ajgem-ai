/** Data explorer: filterable samples/assays, missing-data lists, governing status (FR14). */
import { useMemo, useState } from "react";
import { api, fetchRecordsDetailed, type ConflictView, type RecordView, type SearchResult } from "../lib/api";
import { qualified, recordTitle } from "../lib/format";
import { useAction, useResource } from "../lib/hooks";
import { useProject } from "../components/Layout";
import { SourceDrawer, evidenceItems, type EvidenceItem } from "../components/SourceEvidence";
import { Empty, ErrorNotice, Loading, StatusBadge } from "../components/ui";

export default function Explorer() {
  const { pid, can } = useProject();
  const [kind, setKind] = useState<"assay" | "sample" | "certificate">("assay");
  const [status, setStatus] = useState(can("drafts") ? "" : "approved");
  const [f, setF] = useState({ analyte: "", laboratory: "", campaign: "", method: "", missing: false, text: "" });
  const [dates, setDates] = useState({ from: "", to: "" });
  const dated = useResource(() => fetchRecordsDetailed(pid, { kind, status: status || undefined, date_from: dates.from || undefined, date_to: dates.to || undefined }),
    [pid, kind, status, dates.from, dates.to]);
  const recs = { ...dated, data: dated.data?.records };
  const dateExcluded = dated.data?.dateExcluded ?? null;
  const conflicts = useResource(() => (can("drafts") ? api.get<{ conflicts: ConflictView[] }>(`/projects/${pid}/conflicts`) : Promise.resolve({ conflicts: [] })), [pid]);
  const [drawer, setDrawer] = useState<{ title: string; items: EvidenceItem[] } | null>(null);
  const [q, setQ] = useState("");
  const [search, setSearch] = useState<SearchResult | null>(null);
  const doSearch = useAction(async () => setSearch(await api.get<SearchResult>(`/projects/${pid}/search`, { q })));

  const conflictOf = useMemo(() => {
    const m = new Map<string, { status: string; governing: boolean }>();
    for (const c of conflicts.data?.conflicts ?? []) for (const mem of c.members) m.set(mem.record_id, { status: c.status, governing: mem.governing });
    return m;
  }, [conflicts.data]);

  const values = (key: string) => [...new Set((recs.data ?? []).map((r) => String(r.data?.[key] ?? "")).filter(Boolean))].sort();
  const rows = (recs.data ?? []).filter((r) => {
    const d = r.data ?? {};
    if (f.analyte && d.analyte !== f.analyte) return false;
    if (f.laboratory && d.laboratory !== f.laboratory) return false;
    if (f.campaign && d.campaign !== f.campaign) return false;
    if (f.method && d.method !== f.method) return false;
    if (f.missing && (r.kind !== "sample" || r.geometry)) return false;
    if (f.text && !JSON.stringify(d).toLowerCase().includes(f.text.toLowerCase())) return false;
    return true;
  });

  const openRecord = (r: RecordView) => setDrawer({ title: recordTitle(r), items: evidenceItems(r.field_anchors, r.anchor_ids) });

  return (
    <div className="page">
      <div className="page-head">
        <div>
          <h1>Data explorer</h1>
          <p>{can("drafts") ? "Approved and candidate records" : "Approved records"} with their reported values. Qualifiers are shown exactly (&lt;, &gt;); missing values stay missing.</p>
        </div>
      </div>

      <form className="card card-pad row" onSubmit={(e) => { e.preventDefault(); if (q.trim()) doSearch.run(); }}>
        <label className="field" style={{ flex: 1 }}>Find sample ID, certificate number or text in reports
          <input value={q} onChange={(e) => setQ(e.target.value)} placeholder="KW-001, CERT-001, spodumene…" />
        </label>
        <button style={{ alignSelf: "flex-end" }} disabled={!q.trim()}>Search</button>
      </form>
      <ErrorNotice error={doSearch.error} />
      {search && <SearchResults r={search} onOpenRecord={async (id) => openRecord(await api.get<RecordView>(`/records/${id}`))} />}

      <div className="card">
        <div className="card-pad form-grid" style={{ borderBottom: "1px solid var(--border)" }}>
          <label className="field">Record type
            <select value={kind} onChange={(e) => setKind(e.target.value as typeof kind)}><option value="assay">Assays</option><option value="sample">Samples</option><option value="certificate">Certificates</option></select>
          </label>
          {can("drafts") && (
            <label className="field">Status
              <select value={status} onChange={(e) => setStatus(e.target.value)}><option value="">Any</option><option value="approved">Approved</option><option value="needs_review">Needs review</option><option value="rejected">Rejected</option></select>
            </label>
          )}
          {kind === "assay" && (
            <>
              <label className="field">Analyte<select value={f.analyte} onChange={(e) => setF({ ...f, analyte: e.target.value })}><option value="">Any</option>{values("analyte").map((v) => <option key={v}>{v}</option>)}</select></label>
              <label className="field">Laboratory<select value={f.laboratory} onChange={(e) => setF({ ...f, laboratory: e.target.value })}><option value="">Any</option>{values("laboratory").map((v) => <option key={v}>{v}</option>)}</select></label>
              <label className="field">Method<select value={f.method} onChange={(e) => setF({ ...f, method: e.target.value })}><option value="">Any</option>{values("method").map((v) => <option key={v}>{v}</option>)}</select></label>
            </>
          )}
          <label className="field">Campaign<select value={f.campaign} onChange={(e) => setF({ ...f, campaign: e.target.value })}><option value="">Any</option>{values("campaign").map((v) => <option key={v}>{v}</option>)}</select></label>
          <label className="field">{kind === "certificate" ? "Issued from" : "Collected from"}<input type="date" value={dates.from} onChange={(e) => setDates({ ...dates, from: e.target.value })} /></label>
          <label className="field">to<input type="date" value={dates.to} onChange={(e) => setDates({ ...dates, to: e.target.value })} /></label>
          <label className="field">Contains<input value={f.text} onChange={(e) => setF({ ...f, text: e.target.value })} placeholder="filter rows" /></label>
          {kind === "sample" && <label className="check" style={{ alignSelf: "end" }}><input type="checkbox" checked={f.missing} onChange={(e) => setF({ ...f, missing: e.target.checked })} /> Only samples without approved coordinates</label>}
        </div>
        <div className="card-head">
          <span className="faint">{rows.length} of {recs.data?.length ?? 0} records</span>
          {dateExcluded && (dateExcluded.undated > 0 || dateExcluded.uncertain > 0) && (
            <span className="faint" title={kind === "assay" ? "Assays are dated by the collection date of their sample" : undefined}>
              Not shown: {dateExcluded.undated} with no usable date, {dateExcluded.uncertain} whose month or year only partly overlaps the range
            </span>
          )}
        </div>
        {recs.loading && !recs.data ? <Loading /> : recs.error ? <ErrorNotice error={recs.error} /> : rows.length === 0 ? <Empty>No records match.</Empty> : (
          <div className="table-wrap" style={{ maxHeight: "62vh" }}>
            <table className="data">
              <thead>
                {kind === "assay" ? (
                  <tr><th>Sample</th><th>Analyte</th><th className="num">Result</th><th>Basis</th><th>Method</th><th>Certificate</th><th>Lab</th><th>Status</th><th>Governing</th><th /></tr>
                ) : kind === "sample" ? (
                  <tr><th>Sample</th><th>Campaign</th><th className="num">Raw X</th><th className="num">Raw Y</th><th>Source CRS</th><th>Location</th><th>Status</th><th /></tr>
                ) : (
                  <tr><th>Certificate</th><th>Laboratory</th><th>Status</th><th /></tr>
                )}
              </thead>
              <tbody>
                {rows.slice(0, 2000).map((r) => {
                  const d = r.data ?? {};
                  const c = conflictOf.get(r.id);
                  return (
                    <tr key={r.id} className="clickable" onClick={() => openRecord(r)}>
                      {kind === "assay" ? (
                        <>
                          <td className="mono">{String(d.sample_id ?? "—")}</td>
                          <td>{String(d.analyte ?? "—")}</td>
                          <td className="num">{qualified(d.comparator as string, d.value as string, d.unit as string)}</td>
                          <td>{String(d.basis ?? "—")}</td>
                          <td>{d.method ? String(d.method) : <span className="faint">missing</span>}</td>
                          <td className="mono">{String(d.certificate_number ?? "—")}</td>
                          <td>{String(d.laboratory ?? "—")}</td>
                          <td><StatusBadge status={r.status} />{r.stale && <> <span className="badge warn">stale</span></>}</td>
                          <td>{!c ? <span className="faint">single result</span> : c.governing ? <span className="badge ok">governing</span> : c.status === "open" ? <span className="badge bad">unresolved</span> : c.status === "consistent" ? <span className="badge info">agrees</span> : <span className="badge">alternative</span>}</td>
                        </>
                      ) : kind === "sample" ? (
                        <>
                          <td className="mono">{String(d.sample_id ?? "—")}</td>
                          <td>{String(d.campaign ?? "—")}</td>
                          <td className="num">{String(d.raw_x ?? "—")}</td>
                          <td className="num">{String(d.raw_y ?? "—")}</td>
                          <td className="mono">{d.source_crs ? `${d.source_crs}${d.crs_confirmed ? "" : " (unconfirmed)"}` : <span className="badge warn">unknown</span>}</td>
                          <td>{r.geometry ? <span className="badge ok">mapped</span> : <span className="badge">unmapped</span>}</td>
                          <td><StatusBadge status={r.status} /></td>
                        </>
                      ) : (
                        <>
                          <td className="mono">{String(d.certificate_number ?? "—")}</td>
                          <td>{String(d.laboratory ?? "—")}</td>
                          <td><StatusBadge status={r.status} /></td>
                        </>
                      )}
                      <td><button className="link small" onClick={(e) => { e.stopPropagation(); openRecord(r); }}>Evidence</button></td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </div>
      {drawer && <SourceDrawer title={`Evidence · ${drawer.title}`} items={drawer.items} onClose={() => setDrawer(null)} />}
    </div>
  );
}

function SearchResults({ r, onOpenRecord }: { r: SearchResult; onOpenRecord: (id: string) => void }) {
  return (
    <div className="card">
      <div className="card-head">
        <h2>Results for “{r.query}”</h2>
        <span className="faint">{r.semantic.status === "unavailable" ? "Semantic search unavailable — exact and full-text only" : ""}</span>
      </div>
      <div className="card-pad stack">
        <h3>Exact identifier matches</h3>
        {r.exact.length === 0 ? <div className="faint">None.</div> : r.exact.map((e) => (
          <div key={e.record_id + e.match} className="row">
            <span className="badge">{e.kind}</span>
            <button className="link" onClick={() => onOpenRecord(e.record_id)}>{e.sample_id ?? e.certificate_number}</button>
            <StatusBadge status={e.status} />
            <span className="faint">matched {e.match}</span>
          </div>
        ))}
        <h3>Report text</h3>
        {r.full_text.length === 0 ? <div className="faint">No pages contain this text. (Absence in extracted text is not evidence of absence — scanned pages may lack OCR.)</div> : r.full_text.map((t, i) => (
          <div key={i} className="stack" style={{ gap: 2 }}>
            <span className="faint">Page {t.page_label ?? t.page_index + 1} · version {t.version_id.slice(0, 8)}</span>
            <div className="evidence-text">{t.snippet}</div>
          </div>
        ))}
      </div>
    </div>
  );
}
