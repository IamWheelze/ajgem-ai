/** FR16/FR17 assistant: scoped questions, deterministic results, separated facts/calculations, citations. */
import { useState } from "react";
import { Link } from "react-router-dom";
import { ApiError, api, type Answer, type StructuredAnswer, type ToolResult } from "../lib/api";
import { qualified, reasonText, when } from "../lib/format";
import { useAction, useResource } from "../lib/hooks";
import { useScope } from "../lib/session";
import { useProject } from "../components/Layout";
import { SourceDrawer, type EvidenceItem } from "../components/SourceEvidence";
import { Empty, ErrorNotice, OutcomeBadge, StatusBadge } from "../components/ui";

const EXAMPLES = [
  "Which samples exceed 1000 ppm Li?",
  "Which samples exceed 0.5 % Li2O?",
  "Which results lack coordinates?",
  "Which methods and units were used?",
  "Where was spodumene reported?",
];

interface Snapshot { id: string; state: string; stale_reason: string | null; question: string; params: Record<string, unknown>; result: ToolResult; created_at: string }

export default function Assistant() {
  const { pid, summary } = useProject();
  const { area, setArea } = useScope();
  const [q, setQ] = useState("");
  const [useArea, setUseArea] = useState(true);
  const [basis, setBasis] = useState("");
  const [reportAs, setReportAs] = useState("");
  const [answer, setAnswer] = useState<Answer | null>(null);
  const [snapshot, setSnapshot] = useState<Snapshot | null>(null);
  const [builder, setBuilder] = useState({ analyte: "Li", op: ">", threshold: "1000", unit: "ppm" });
  const history = useResource(() => api.get<{ answers: { id: string; question: string; state: string; stale_reason: string | null; created_at: string; counts: Record<string, number> | null }[] }>(`/projects/${pid}/answers`), [pid, answer]);
  const [drawer, setDrawer] = useState<{ title: string; items: EvidenceItem[] } | null>(null);

  const scope = () => ({
    ...(area && useArea ? { sample_keys: area.sampleKeys } : {}),
    ...(basis ? { basis } : {}),
    ...(reportAs ? { report_as: reportAs } : {}),
  });
  const ask = useAction(async (question: string) => {
    setSnapshot(null);
    setAnswer(await api.post<Answer>(`/projects/${pid}/questions`, { question, params: scope() }));
  });
  const runBuilder = useAction(async () => {
    setSnapshot(null);
    setAnswer(await api.post<Answer>(`/projects/${pid}/questions`, { tool: "assay_threshold", question: `Which samples have ${builder.analyte} ${builder.op} ${builder.threshold} ${builder.unit}?`, params: { ...builder, ...scope() } }));
  });
  const [reproduced, setReproduced] = useState<boolean | null>(null);
  const openSnap = useAction(async (id: string) => { setAnswer(null); setReproduced(null); setSnapshot(await api.get<Snapshot>(`/answers/${id}`)); });
  const rerun = useAction(async (id: string) => api.post<{ reproduced: boolean; result: ToolResult }>(`/answers/${id}/rerun`));
  const err = ask.error ?? runBuilder.error;

  const cite = (title: string, ids: string[]) => setDrawer({ title, items: ids.map((anchorId) => ({ anchorId })) });

  return (
    <div className="page">
      <div className="page-head">
        <div>
          <h1>Assistant</h1>
          <p>Numerical questions run allowlisted, read-only tools over <strong>approved</strong> records — no generated SQL. Each answer shows source facts and calculations separately, and every row links to its evidence.</p>
        </div>
      </div>

      <form className="card card-pad stack" onSubmit={(e) => { e.preventDefault(); if (q.trim()) ask.run(q); }}>
        <div className="ask-box">
          <input value={q} onChange={(e) => setQ(e.target.value)} placeholder="Ask about this project's evidence…" aria-label="Question" />
          <button className="primary" disabled={!q.trim() || ask.pending}>{ask.pending ? "Working…" : "Ask"}</button>
        </div>
        <div className="row">
          <span className="faint">Scope:</span>
          <div className="chips">
            <span className="chip on">Project · {summary.project.name}</span>
            {area ? (
              <button type="button" className={`chip ${useArea ? "on" : ""}`} onClick={() => setUseArea((u) => !u)} aria-pressed={useArea}>Map area · {area.sampleIds.length} samples</button>
            ) : (
              <Link className="chip" to={`/p/${pid}/map`}>+ Select a map area</Link>
            )}
            {area && <button type="button" className="chip" onClick={() => setArea(null)}>✕ clear area</button>}
            <select className="chip" value={basis} onChange={(e) => setBasis(e.target.value)} aria-label="Basis"><option value="">Basis · any</option><option value="dry">Basis · dry</option><option value="as_received">Basis · as received</option></select>
            <select className="chip" value={reportAs} onChange={(e) => setReportAs(e.target.value)} aria-label="Report as"><option value="">Report as reported</option><option value="Li2O">Also report Li2O equivalent</option></select>
          </div>
        </div>
        <div className="chips">
          {EXAMPLES.map((x) => <button type="button" key={x} className="chip" onClick={() => { setQ(x); ask.run(x); }}>{x}</button>)}
        </div>
        <details>
          <summary>Threshold builder (explicit analyte, unit and operator)</summary>
          <div className="row" style={{ marginTop: 8 }}>
            <input style={{ width: 90 }} value={builder.analyte} onChange={(e) => setBuilder({ ...builder, analyte: e.target.value })} aria-label="Analyte" />
            <select value={builder.op} onChange={(e) => setBuilder({ ...builder, op: e.target.value })} aria-label="Operator">{[">", ">=", "<", "<="].map((o) => <option key={o}>{o}</option>)}</select>
            <input style={{ width: 110 }} className="mono" value={builder.threshold} onChange={(e) => setBuilder({ ...builder, threshold: e.target.value })} aria-label="Threshold" />
            <select value={builder.unit} onChange={(e) => setBuilder({ ...builder, unit: e.target.value })} aria-label="Unit">{["ppm", "ppb", "%", "g/t"].map((u) => <option key={u}>{u}</option>)}</select>
            <button type="button" onClick={runBuilder.run} disabled={runBuilder.pending}>Run</button>
          </div>
        </details>
      </form>

      {err && <AnswerError error={err} onBasis={(b) => { setBasis(b); }} />}
      {answer?.kind === "structured" && <StructuredView a={answer} onCite={cite} />}
      {answer?.kind === "retrieval_only" && (
        <div className="card card-pad stack">
          <div className="notice info"><div><strong>No generated answer</strong>{answer.message}</div></div>
          {answer.insufficient_evidence && <div className="notice warn"><div><strong>Insufficient evidence</strong>Nothing in the authorised, extracted text matches. This is not evidence that it is absent from the original documents.</div></div>}
          {answer.search.full_text.map((t, i) => (
            <div key={i} className="stack" style={{ gap: 2 }}>
              <span className="faint">Source statement · page {t.page_label ?? t.page_index + 1} · version {t.version_id.slice(0, 8)}</span>
              <div className="evidence-text">{t.snippet}</div>
            </div>
          ))}
        </div>
      )}
      {snapshot && (
        <div className="stack">
          <div className={`notice ${snapshot.state === "current" ? "info" : "warn"}`}>
            <div>
              <strong>Saved answer · {snapshot.question}</strong>
              Asked {when(snapshot.created_at)} · <StatusBadge status={snapshot.state} /> {snapshot.stale_reason && <span className="faint">— {snapshot.stale_reason}</span>}
              <div className="row" style={{ marginTop: 6 }}>
                <button className="small" onClick={async () => { const r = await rerun.run(snapshot.id); if (r) setReproduced(r.reproduced); }}>Re-run frozen snapshot</button>
                <button className="small primary" onClick={() => { setSnapshot(null); setAnswer(null); api.post<Answer>(`/projects/${pid}/questions`, { tool: "assay_threshold", question: snapshot.question, params: snapshot.params }).then(setAnswer, () => undefined); }}>Fresh calculation on current data</button>
              </div>
              {reproduced !== null && <div style={{ marginTop: 6 }}>{reproduced ? <span className="badge ok">Frozen inputs reproduce the stored result exactly</span> : <span className="badge bad">Re-run differs from the stored result</span>}</div>}
            </div>
          </div>
          <ThresholdView r={snapshot.result} onCite={cite} />
        </div>
      )}

      <section className="card">
        <div className="card-head"><h2>Answer history</h2><span className="faint">Answers are frozen snapshots; they go stale when evidence or governing results change.</span></div>
        {!history.data?.answers.length ? <Empty>No saved answers.</Empty> : (
          <table className="data">
            <thead><tr><th>Question</th><th>Result</th><th>State</th><th>Asked</th></tr></thead>
            <tbody>
              {history.data.answers.map((h) => (
                <tr key={h.id} className="clickable" onClick={() => openSnap.run(h.id)}>
                  <td>{h.question}</td>
                  <td className="faint">{h.counts ? `${h.counts.YES} meet · ${h.counts.INDETERMINATE} indeterminate` : ""}</td>
                  <td><StatusBadge status={h.state} />{h.stale_reason && <div className="faint">{h.stale_reason}</div>}</td>
                  <td className="faint">{when(h.created_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>
      {drawer && <SourceDrawer title={drawer.title} items={drawer.items} onClose={() => setDrawer(null)} />}
    </div>
  );
}

function AnswerError({ error, onBasis }: { error: unknown; onBasis: (b: string) => void }) {
  if (error instanceof ApiError && error.code === "CLARIFICATION_REQUIRED") {
    const bases = (error.details as { bases?: string[] } | undefined)?.bases;
    return (
      <div className="notice warn">
        <div>
          <strong>Clarification needed</strong>{error.message}
          {bases && <div className="row" style={{ marginTop: 6 }}>{bases.map((b) => <button key={b} className="small" onClick={() => onBasis(b)}>Use basis: {b}</button>)}<span className="faint">then ask again</span></div>}
        </div>
      </div>
    );
  }
  if (error instanceof ApiError && error.code === "CONVERSION_NOT_APPROVED")
    return <div className="notice warn"><div><strong>Conversion factor not approved</strong>{error.message}. A reviewer must activate the versioned factor (Administration → Conversion factors) before oxide equivalents can be calculated.</div></div>;
  return <ErrorNotice error={error} />;
}

function StructuredView({ a, onCite }: { a: StructuredAnswer; onCite: (t: string, ids: string[]) => void }) {
  const r = a.result;
  return (
    <div className="stack">
      <div className="row">
        {a.state && <StatusBadge status={a.state} label={`answer ${a.state}`} />}
        <span className={`badge ${a.citations.supported ? "ok" : "bad"}`}>{a.citations.supported ? `${a.citations.anchor_ids.length} citations resolve` : `${a.citations.unresolved.length} citations unavailable`}</span>
        <span className="faint">{r.tool} v{r.tool_version}</span>
      </div>
      {a.insufficient_evidence && <div className="notice warn"><div><strong>Insufficient evidence</strong>No approved records satisfy this query's scope. This is not a statement that none exist in unreviewed or unextracted material.</div></div>}
      {r.tool === "assay_threshold" ? <ThresholdView r={r} onCite={onCite} /> : <GenericTool r={r} onCite={onCite} />}
      <div className="card card-pad">
        <div className="section-label"><span className="bar" style={{ background: "var(--indet)" }} /><h3>Interpretations</h3></div>
        <p className="faint">None. The system does not add geological interpretation to calculated results; interpretations must be authored and attributed by a geologist.</p>
      </div>
    </div>
  );
}

function ThresholdView({ r, onCite }: { r: ToolResult; onCite: (t: string, ids: string[]) => void }) {
  const q = r.query!;
  const [show, setShow] = useState<"YES" | "INDETERMINATE" | "NO" | "ALL">("YES");
  const rows = r.rows.filter((x) => show === "ALL" || x.outcome === show);
  return (
    <>
      <div className="card">
        <div className="card-head">
          <div className="section-label"><span className="bar" style={{ background: "var(--accent)" }} /><h2>Calculated result: {q.analyte} {q.op} {q.threshold} {q.unit}{q.basis ? ` (${q.basis})` : ""}</h2></div>
        </div>
        <div className="card-pad stack">
          <div className="chips" role="group" aria-label="Outcome filter">
            {(["YES", "INDETERMINATE", "NO", "ALL"] as const).map((k) => (
              <button key={k} className={`chip ${show === k ? "on" : ""}`} onClick={() => setShow(k)} aria-pressed={show === k}>
                {k === "ALL" ? `All ${r.rows.length}` : `${k === "YES" ? "Meets" : k === "NO" ? "Does not meet" : "Indeterminate"} · ${r.counts?.[k] ?? 0}`}
              </button>
            ))}
          </div>
          {(r.counts?.INDETERMINATE ?? 0) > 0 && <div className="faint">Indeterminate: a censored value (e.g. &lt;100 ppm) whose bound straddles the threshold. It is never substituted with a number.</div>}
          {(r.unmapped_matching_samples ?? 0) > 0 && <div className="faint">{r.unmapped_matching_samples} matching sample(s) have no approved location and are not on the map.</div>}
        </div>
        {rows.length === 0 ? <Empty>No rows in this category.</Empty> : (
          <div className="table-wrap" style={{ maxHeight: 440 }}>
            <table className="data">
              <thead><tr><th>Sample</th><th className="num">Reported</th><th className="num">Compared ({q.unit})</th><th>Outcome</th>{r.rows.some((x) => x.equivalent) && <th className="num">Equivalent</th>}<th>Certificate</th><th>Method</th><th>Rev</th><th>Map</th><th>Evidence</th></tr></thead>
              <tbody>
                {rows.map((x) => (
                  <tr key={x.record_id}>
                    <td className="mono">{x.sample_id}{x.stale && <> <span className="badge warn">stale</span></>}</td>
                    <td className="num">{x.result_raw} {x.unit}</td>
                    <td className="num" title={x.derived ?? undefined}>{qualified(x.comparator, x.compared_value)}{x.derived && " *"}</td>
                    <td><OutcomeBadge outcome={x.outcome} /></td>
                    {r.rows.some((y) => y.equivalent) && <td className="num" title={x.equivalent ? `${x.equivalent.exact_pct}% exact · factor ${x.equivalent.factor} (${x.equivalent.factor_version}) · ${x.equivalent.rounding}` : undefined}>{x.equivalent ? `${qualified(x.equivalent.comparator, x.equivalent.display_pct)}% ${x.equivalent.analyte}` : "—"}</td>}
                    <td className="mono">{x.certificate_number ?? "—"}</td>
                    <td>{x.method ?? <span className="faint">missing</span>}</td>
                    <td className="faint">r{x.revision_no}</td>
                    <td>{x.mapped ? <span className="badge ok">mapped</span> : <span className="badge">unmapped</span>}</td>
                    <td><button className="link small" onClick={() => onCite(`Evidence · ${x.sample_id}`, x.anchor_ids)}>{x.anchor_ids.length} source{x.anchor_ids.length === 1 ? "" : "s"}</button></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>

      {(r.exclusions?.length ?? 0) > 0 && (
        <div className="card">
          <div className="card-head"><div className="section-label"><span className="bar" style={{ background: "var(--warn)" }} /><h2>Excluded from the calculation · {r.exclusions!.length}</h2></div></div>
          <table className="data">
            <thead><tr><th>Sample</th><th>Certificate</th><th>Reason</th></tr></thead>
            <tbody>
              {r.exclusions!.map((e) => (
                <tr key={e.record_id}>
                  <td className="mono">{e.sample_id}</td>
                  <td className="mono">{e.certificate_number ?? "—"}</td>
                  <td>{reasonText(e.reason)} {e.conflict_group_id && <Link to="../conflicts" relative="path">review conflict</Link>}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      <details className="card card-pad">
        <summary>How this was calculated</summary>
        <div className="stack" style={{ marginTop: 8 }}>
          <div className="faint">Tool {r.tool} v{r.tool_version}. Censoring: {String(r.calculation?.censoring_policy)}. Scope: {String(r.calculation?.scope)}. Registries: {String(r.calculation?.unit_registry)}, {String(r.calculation?.analyte_registry)}.</div>
          {r.rows.some((x) => x.derived) && <div className="faint">* {r.rows.find((x) => x.derived)!.derived}.</div>}
          <pre className="json">{JSON.stringify({ query: r.query, calculation: r.calculation }, null, 2)}</pre>
        </div>
      </details>
    </>
  );
}

function GenericTool({ r, onCite }: { r: ToolResult; onCite: (t: string, ids: string[]) => void }) {
  const cols = r.rows.length ? Object.keys(r.rows[0]).filter((k) => !["record_ids", "anchor_ids", "revision_id", "record_id"].includes(k)) : [];
  return (
    <div className="card">
      <div className="card-head"><div className="section-label"><span className="bar" style={{ background: "var(--accent)" }} /><h2>{r.tool.replaceAll("_", " ")} · {r.rows.length}</h2></div>{r.note && <span className="faint">{r.note}</span>}</div>
      {r.rows.length === 0 ? <Empty>No rows.</Empty> : (
        <table className="data">
          <thead><tr>{cols.map((c) => <th key={c}>{c.replaceAll("_", " ")}</th>)}{r.rows[0].anchor_ids && <th>Evidence</th>}</tr></thead>
          <tbody>
            {r.rows.map((x, i) => (
              <tr key={i}>
                {cols.map((c) => <td key={c} className="mono">{x[c] === null || x[c] === undefined ? <span className="faint">missing</span> : String(x[c])}</td>)}
                {x.anchor_ids && <td><button className="link small" onClick={() => onCite("Evidence", x.anchor_ids)}>{x.anchor_ids.length} sources</button></td>}
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}
