import { useRef, useState } from "react";
import { Link } from "react-router-dom";
import { ApiError, api, type DocumentList, type JobView } from "../lib/api";
import { bytes, when } from "../lib/format";
import { RUNNING_JOB_STATES, usePolling, useResource } from "../lib/hooks";
import { completeUpload, sendFile, type UploadResult, type UploadSession } from "../lib/upload";
import { useProject } from "../components/Layout";
import { DeleteForm, TombstoneSummary, type Tombstone } from "../components/DeleteForm";
import { ImportMapper } from "../components/ImportMapper";
import { Empty, ErrorNotice, StatusBadge } from "../components/ui";

type Phase = "choose" | "uploading" | "paused" | "decide" | "done";

export default function Upload() {
  const { pid, can, reload, roles } = useProject();
  const [deleting, setDeleting] = useState<string | null>(null);
  const [tomb, setTomb] = useState<Tombstone | null>(null);
  const [file, setFile] = useState<File | null>(null);
  const [rights, setRights] = useState("");
  const [confirmed, setConfirmed] = useState(false);
  const [phase, setPhase] = useState<Phase>("choose");
  const [progress, setProgress] = useState<{ sent: number; total: number; step: string }>({ sent: 0, total: 0, step: "" });
  const [session, setSession] = useState<UploadSession | null>(null);
  const [result, setResult] = useState<UploadResult | null>(null);
  const [candidates, setCandidates] = useState<string[]>([]);
  const [error, setError] = useState<unknown>();
  const [over, setOver] = useState(false);
  const abort = useRef<AbortController | null>(null);
  const docs = useResource(() => api.get<DocumentList>(`/projects/${pid}/documents`), [pid, result, tomb]);

  if (!can("upload")) return <div className="page"><ErrorNotice error={new Error("Uploading requires the analyst or field-contributor role on this project.")} title="Not permitted" /></div>;

  async function start() {
    if (!file) return;
    setError(undefined);
    setPhase("uploading");
    abort.current = new AbortController();
    try {
      const s = await sendFile({
        projectId: pid, file, rightsStatement: rights, signal: abort.current.signal,
        onProgress: (sent, total, step) => setProgress({ sent, total, step }),
      });
      setSession(s);
      await finish(s.id, {});
    } catch (e) {
      if ((e as Error).name === "AbortError") setPhase("paused");
      else { setError(e); setPhase("paused"); }
    }
  }

  async function finish(sessionId: string, opts: { replaces_document_id?: string; as_new_document?: boolean }) {
    try {
      const r = await completeUpload(sessionId, opts);
      setResult(r);
      setPhase("done");
      reload();
    } catch (e) {
      if (e instanceof ApiError && e.code === "REPLACEMENT_AMBIGUOUS") {
        setCandidates(((e.details as { candidates?: string[] })?.candidates) ?? []);
        setPhase("decide");
      } else {
        setError(e);
        setPhase("paused");
      }
    }
  }

  function reset() {
    setFile(null); setResult(null); setSession(null); setPhase("choose"); setError(undefined); setConfirmed(false);
  }

  const pct = progress.total ? Math.round((progress.sent / progress.total) * 100) : 0;
  const stepCls = (k: Phase[]) => (k.includes(phase) ? "step cur" : "step");
  return (
    <div className="page">
      <div className="page-head">
        <div>
          <h1>Upload & import</h1>
          <p>Files are sent in verified chunks and resume after a dropped connection — choose the same file again to continue. Originals are stored immutably; extraction only ever produces candidates for review.</p>
        </div>
      </div>
      <div className="steps" aria-label="Progress">
        <span className={phase === "choose" ? "step cur" : "step done"}>1 Choose & declare rights</span>
        <span className={stepCls(["uploading", "paused", "decide"])}>2 Transfer & verify</span>
        <span className={phase === "done" ? "step cur" : "step"}>3 Process or map columns</span>
      </div>

      {phase === "choose" && (
        <div className="card card-pad stack">
          <div
            className={`dropzone ${over ? "over" : ""}`}
            onDragOver={(e) => { e.preventDefault(); setOver(true); }}
            onDragLeave={() => setOver(false)}
            onDrop={(e) => { e.preventDefault(); setOver(false); setFile(e.dataTransfer.files[0] ?? null); }}
          >
            <p><strong>Drop a PDF report or certificate, CSV or XLSX table</strong></p>
            <p className="faint">Limit 100 MB · 500 PDF pages · 100,000 table rows. Macro-enabled workbooks and password-protected PDFs are rejected.</p>
            <input type="file" accept=".pdf,.csv,.tsv,.txt,.xlsx" onChange={(e) => setFile(e.target.files?.[0] ?? null)} aria-label="Choose file" />
            {file && <p><strong>{file.name}</strong> · {bytes(file.size)}</p>}
          </div>
          <label className="field">
            Rights statement (who owns this material and on what basis you may process it)
            <input value={rights} onChange={(e) => setRights(e.target.value)} placeholder="e.g. Client report supplied under NDA 2026-14 for this project only" />
          </label>
          <label className="check">
            <input type="checkbox" checked={confirmed} onChange={(e) => setConfirmed(e.target.checked)} />
            I confirm I have the right to upload and process this material for this project. Uploading indexes it; it does not train a shared model.
          </label>
          <div className="row"><button className="primary" disabled={!file || !confirmed || !rights.trim()} onClick={start}>Upload</button></div>
        </div>
      )}

      {(phase === "uploading" || phase === "paused") && (
        <div className="card card-pad stack">
          <div className="row between">
            <strong>{file?.name}</strong>
            <span className="faint">{progress.step === "hashing" ? "Computing checksum…" : progress.step === "verifying" ? "Verifying…" : `${bytes(progress.sent)} of ${bytes(progress.total)}`}</span>
          </div>
          <div className="progress" role="progressbar" aria-valuenow={pct} aria-valuemin={0} aria-valuemax={100}><div style={{ width: `${pct}%` }} /></div>
          <ErrorNotice error={error} />
          <div className="row">
            {phase === "uploading" ? (
              <button onClick={() => abort.current?.abort()}>Pause</button>
            ) : (
              <>
                <button className="primary" onClick={start}>Resume</button>
                <button onClick={reset}>Choose another file</button>
              </>
            )}
            <span className="faint">Confirmed ranges are kept on the server for 7 days.</span>
          </div>
        </div>
      )}

      {phase === "decide" && session && (
        <div className="card card-pad stack">
          <div className="notice warn"><div><strong>A document named “{file?.name}” already exists</strong>Choose whether this file is a new version of it or a separate document. A replacement marks records from the old version as stale; it does not make them disappear.</div></div>
          <div className="row">
            {candidates.map((d) => {
              const doc = docs.data?.documents.find((x) => x.id === d);
              return <button key={d} onClick={() => finish(session.id, { replaces_document_id: d })}>Replace “{doc?.title ?? d.slice(0, 8)}” (new version {((doc?.versions.length ?? 0) + 1)})</button>;
            })}
            <button onClick={() => finish(session.id, { as_new_document: true })}>Keep as a separate document</button>
          </div>
        </div>
      )}

      {phase === "done" && result && <Outcome result={result} onReset={reset} />}

      {tomb && <TombstoneSummary t={tomb} />}
      <section className="card">
        <div className="card-head"><h2>Source documents</h2></div>
        {!docs.data?.documents.length ? <Empty>No documents yet.</Empty> : (
          <div className="table-wrap">
            <table className="data">
              <thead><tr><th>Document</th><th>Version</th><th>Format</th><th>Status</th><th>Scan</th><th className="num">Size</th><th>SHA-256</th><th>Uploaded</th>{roles.has("admin") && <th />}</tr></thead>
              <tbody>
                {docs.data.documents.flatMap((d) => d.versions.map((v) => [
                  <tr key={v.id}>
                    <td>{d.title}</td>
                    <td>v{v.version_no}</td>
                    <td>{v.format}</td>
                    <td><StatusBadge status={v.status} /></td>
                    <td className="faint">{v.scan_status.replaceAll("_", " ")}</td>
                    <td className="num">{bytes(v.size)}</td>
                    <td className="mono faint" title={v.sha256}>{v.sha256.slice(0, 12)}…</td>
                    <td className="faint">{when(v.created_at)}</td>
                    {roles.has("admin") && <td>{v.status !== "deleted" && <button className="small danger" onClick={() => { setTomb(null); setDeleting(deleting === v.id ? null : v.id); }}>Delete…</button>}</td>}
                  </tr>,
                  deleting === v.id && (
                    <tr key={`${v.id}-del`}><td colSpan={9}>
                      <DeleteForm url={`/versions/${v.id}`} what={`“${v.filename}” v${v.version_no}`} confirmText={v.filename}
                        onCancel={() => setDeleting(null)} onDone={(t) => { setDeleting(null); setTomb(t); reload(); }} />
                    </td></tr>
                  ),
                ]))}
              </tbody>
            </table>
          </div>
        )}
      </section>
    </div>
  );
}

function Outcome({ result, onReset }: { result: UploadResult; onReset: () => void }) {
  const { pid } = useProject();
  if (result.status === "duplicate")
    return (
      <div className="notice info"><div><strong>Already uploaded</strong>{result.message}. No new version was created. <button className="link" onClick={onReset}>Upload another file</button></div></div>
    );
  if (result.status === "quarantined")
    return <div className="notice bad"><div><strong>Quarantined</strong>The file failed malware scanning ({result.reason}) and will not be parsed.</div></div>;
  return (
    <div className="stack">
      <div className="notice ok">
        <div>
          <strong>Stored as version {result.version_no} ({result.format.toUpperCase()})</strong>
          {result.supersedes && <> Supersedes the previous version; {result.impact.length} record(s) from it are now marked stale for re-review.</>}
          {" "}<button className="link" onClick={onReset}>Upload another file</button>
        </div>
      </div>
      {result.format === "pdf" && result.job_id && <JobPanel jobId={result.job_id} />}
      {(result.format === "csv" || result.format === "xlsx") && <ImportMapper versionId={result.version_id} format={result.format} />}
      <p className="faint">Next: <Link to={`/p/${pid}/review`}>review the extracted candidates</Link>.</p>
    </div>
  );
}

export function JobPanel({ jobId }: { jobId: string }) {
  const job = useResource(() => api.get<JobView>(`/jobs/${jobId}`), [jobId]);
  const [err, setErr] = useState<unknown>();
  usePolling(RUNNING_JOB_STATES.includes(job.data?.state ?? ""), job.reload);
  if (!job.data) return null;
  const j = job.data;
  const retry = async () => {
    try { job.setData(await api.post<JobView>(`/jobs/${jobId}/retry`, { units: j.retryable })); } catch (e) { setErr(e); }
  };
  return (
    <div className="card">
      <div className="card-head">
        <h2>Processing manifest</h2>
        <div className="row">{RUNNING_JOB_STATES.includes(j.state) && <span className="faint">Processing on a worker; this updates by itself.</span>}<StatusBadge status={j.state} />{j.retryable.length > 0 && <button className="small" onClick={retry}>Retry {j.retryable.length} failed</button>}</div>
      </div>
      <ErrorNotice error={err} />
      <BudgetPanel job={j} onChange={job.setData} />
      <div className="table-wrap" style={{ maxHeight: 320 }}>
        <table className="data">
          <thead><tr><th>Unit</th><th>State</th><th className="num">Attempts</th><th className="num">Candidates</th><th>Issues</th></tr></thead>
          <tbody>
            {j.manifest.units?.map((u) => (
              <tr key={u.unit}>
                <td className="mono">{u.unit.replace("page:", "page ").replace(/ 0+(\d)/, " $1")}</td>
                <td><StatusBadge status={u.state} /></td>
                <td className="num">{u.attempts}</td>
                <td className="num">{u.candidates}</td>
                <td>{u.issues.map((i) => <div key={i.code}><code>{i.code}</code> <span className="faint">{i.message}</span></div>)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {j.manifest.note && <div className="card-pad faint">{j.manifest.note}</div>}
    </div>
  );
}

function BudgetPanel({ job, onChange }: { job: JobView; onChange: (j: JobView) => void }) {
  const { roles } = useProject();
  const [reason, setReason] = useState("");
  const [err, setErr] = useState<unknown>();
  const m = job.manifest;
  if (!m.estimate && !m.budget_override) return null;
  const approve = async () => {
    try { onChange(await api.post<JobView>(`/jobs/${job.id}/budget-override`, { reason })); } catch (e) { setErr(e); }
  };
  return (
    <div className="card-pad stack" style={{ borderBottom: "1px solid var(--border)" }}>
      {m.estimate && <div className="faint">Estimate before processing: {m.estimate.pages} page(s), up to {m.estimate.max_ocr_pages} by OCR{m.estimate.model_assisted ? "; tables the pattern reader cannot parse may be sent to the language model" : ""}.</div>}
      {job.state === "awaiting_budget_approval" && (
        <>
          <div><strong>Waiting for budget approval.</strong> {m.budget?.join(" ")}</div>
          {roles.has("admin") ? (
            <div className="row">
              <label className="field" style={{ flex: 1 }}>Reason for processing above budget (recorded in the audit log)
                <input value={reason} onChange={(e) => setReason(e.target.value)} placeholder="e.g. certificate needed for the board pack" />
              </label>
              <button className="primary" style={{ alignSelf: "flex-end" }} disabled={reason.trim().length < 10} onClick={approve}>Approve and process</button>
            </div>
          ) : <div className="faint">A project administrator can approve it with a reason, or raise the quota.</div>}
          <ErrorNotice error={err} />
        </>
      )}
      {m.budget_override && <div className="faint">Approved above budget: “{m.budget_override.reason}” ({when(m.budget_override.at)})</div>}
    </div>
  );
}
