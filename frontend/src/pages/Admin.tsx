/** Administration: members and grants, project configuration, conversion factors, audit (FR01, FR02, FR21). */
import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { api, type DocumentList, type ProjectDetail, type UsageStatus } from "../lib/api";
import { DeleteForm, TombstoneSummary, type Tombstone } from "../components/DeleteForm";
import { ROLE_LABEL, bytes, when } from "../lib/format";
import { useAction, useResource } from "../lib/hooks";
import { useProject } from "../components/Layout";
import { Empty, ErrorNotice, Loading } from "../components/ui";

interface Member { user_id: string; email: string; roles: string[]; can_export: boolean; can_export_exact_coordinates: boolean; revoked: boolean; expires_at: string | null }
interface AuditEv { id: string; at: string; actor: string; action: string; entity_type: string; entity_id: string; before: string | null; after: string | null; detail: Record<string, unknown> }
interface Factor { element: string; oxide: string; factor: string; version: string; provenance: string; approved: boolean; approved_by: string | null }

export default function Admin() {
  const { roles } = useProject();
  if (!roles.has("admin") && !roles.has("reviewer")) return <div className="page"><ErrorNotice error={new Error("Administration requires the administrator role.")} title="Not permitted" /></div>;
  return (
    <div className="page">
      <div className="page-head"><div><h1>Administration</h1><p>Permissions are per-project grants. Administration does not confer geological review authority.</p></div></div>
      {roles.has("admin") && <Members />}
      {roles.has("admin") && <Config />}
      {roles.has("admin") && <LanguageModelPolicy />}
      {roles.has("admin") && <UsageAndBudget />}
      <Factors />
      {roles.has("admin") && <Audit />}
      {roles.has("admin") && <Documents />}
      {roles.has("admin") && <DangerZone />}
    </div>
  );
}

function Members() {
  const { pid } = useProject();
  const list = useResource(() => api.get<{ members: Member[] }>(`/projects/${pid}/members`), [pid]);
  const users = useResource(() => api.get<{ users: { id: string; email: string; name: string }[] }>("/org/users"), []);
  const [uid, setUid] = useState("");
  const [rs, setRs] = useState<Set<string>>(new Set(["viewer"]));
  const [exp, setExp] = useState(false);
  const [exact, setExact] = useState(false);
  const add = useAction(async () => {
    await api.post(`/projects/${pid}/members`, { user_id: uid, roles: [...rs], can_export: exp, can_export_exact_coordinates: exact });
    list.reload();
  });
  const revoke = useAction(async (u: string) => { await api.del(`/projects/${pid}/members/${u}`); list.reload(); });
  const emailOf = new Map(users.data?.users.map((u) => [u.id, u.email]));
  return (
    <section className="card">
      <div className="card-head"><h2>Members</h2></div>
      {list.loading && !list.data ? <Loading /> : (
        <table className="data">
          <thead><tr><th>User</th><th>Roles</th><th>Export</th><th>Status</th><th /></tr></thead>
          <tbody>
            {list.data?.members.map((m) => (
              <tr key={m.user_id}>
                <td>{m.email}</td>
                <td>{m.roles.map((r) => <span key={r} className="badge accent" style={{ marginRight: 4 }}>{ROLE_LABEL[r] ?? r}</span>)}</td>
                <td>{m.can_export ? (m.can_export_exact_coordinates ? "yes, incl. exact coordinates" : "yes") : <span className="faint">no</span>}</td>
                <td>{m.revoked ? <span className="badge bad">revoked</span> : <span className="badge ok">active</span>}{m.expires_at && <div className="faint">expires {when(m.expires_at)}</div>}</td>
                <td>{!m.revoked && <button className="small danger" onClick={() => { if (confirm(`Revoke ${m.email}? Access ends immediately.`)) revoke.run(m.user_id); }}>Revoke</button>}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      <form className="card-pad stack" style={{ borderTop: "1px solid var(--border)" }} onSubmit={(e) => { e.preventDefault(); add.run(); }}>
        <h3>Grant access</h3>
        <div className="row" style={{ alignItems: "flex-end" }}>
          <label className="field">User (same organisation)
            <select value={uid} onChange={(e) => setUid(e.target.value)}>
              <option value="">Choose…</option>
              {users.data?.users.map((u) => <option key={u.id} value={u.id}>{u.email}</option>)}
            </select>
          </label>
          {Object.keys(ROLE_LABEL).map((r) => (
            <label key={r} className="check"><input type="checkbox" checked={rs.has(r)} onChange={(e) => { const s = new Set(rs); if (e.target.checked) s.add(r); else s.delete(r); setRs(s); }} />{ROLE_LABEL[r]}</label>
          ))}
        </div>
        <div className="row">
          <label className="check"><input type="checkbox" checked={exp} onChange={(e) => setExp(e.target.checked)} />Export grant</label>
          <label className="check"><input type="checkbox" checked={exact} disabled={!exp} onChange={(e) => setExact(e.target.checked)} />Exact coordinates in exports</label>
          <button className="primary" disabled={!uid || rs.size === 0 || add.pending}>Grant</button>
        </div>
        <ErrorNotice error={add.error ?? revoke.error} />
        {uid && emailOf.get(uid) && <span className="faint">Granting updates an existing membership's roles if {emailOf.get(uid)} is already a member.</span>}
      </form>
    </section>
  );
}

function Config() {
  const { pid } = useProject();
  const p = useResource(() => api.get<ProjectDetail>(`/projects/${pid}`), [pid]);
  const [crs, setCrs] = useState<string | null>(null);
  const [boundary, setBoundary] = useState<string | null>(null);
  const save = useAction(async () => {
    let b: unknown = undefined;
    if (boundary !== null && boundary.trim()) {
      try { b = JSON.parse(boundary); } catch { throw new Error("Boundary is not valid JSON"); }
    }
    await api.put(`/projects/${pid}/config`, { working_crs: crs ?? undefined, boundary: b });
    setCrs(null); setBoundary(null); p.reload();
  });
  const cfg = p.data?.config;
  return (
    <section className="card">
      <div className="card-head"><h2>Project configuration</h2>{cfg && <span className="faint">revision {cfg.revision} · {when(cfg.created_at)}</span>}</div>
      <div className="card-pad stack">
        <label className="field">Working CRS (for analysis; never assigned to input data)
          <input value={crs ?? cfg?.working_crs ?? ""} onChange={(e) => setCrs(e.target.value)} placeholder="EPSG:32632" />
        </label>
        <label className="field">Boundary (GeoJSON Polygon/MultiPolygon, WGS 84 lon/lat; administrative context, not proof of rights)
          <textarea rows={6} value={boundary ?? (cfg?.boundary ? JSON.stringify(cfg.boundary) : "")} onChange={(e) => setBoundary(e.target.value)} />
        </label>
        <ErrorNotice error={save.error} />
        <div><button className="primary" disabled={(crs === null && boundary === null) || save.pending} onClick={save.run}>Save as new revision</button></div>
      </div>
    </section>
  );
}

function LanguageModelPolicy() {
  const { pid } = useProject();
  const p = useResource(() => api.get<ProjectDetail>(`/projects/${pid}`), [pid]);
  const policy = p.data?.config?.provider_policy ?? {};
  const [allow, setAllow] = useState<boolean | null>(null);
  const [notes, setNotes] = useState<string | null>(null);
  const llm = p.data?.llm;
  const on = allow ?? Boolean(policy.allow_external_llm);
  const text = notes ?? policy.notes ?? "";
  const save = useAction(async () => {
    await api.put(`/projects/${pid}/config`, { provider_policy: { allow_external_llm: on, provider: llm?.provider ?? null, notes: text } });
    setAllow(null); setNotes(null); p.reload();
  });
  return (
    <section className="card">
      <div className="card-head">
        <h2>Language model (answers and extraction)</h2>
        {policy.allow_external_llm ? <span className="badge ok">allowed</span> : <span className="badge">off</span>}
      </div>
      <div className="card-pad stack">
        <p className="muted" style={{ margin: 0 }}>
          Narrative questions can be answered by an external language model that sees only the authorised document excerpts
          retrieval selects. Every statement must cite those excerpts; uncited text and figures not found in the cited text are withheld.
          Numerical questions always use the deterministic tools.
        </p>
        <p className="muted" style={{ margin: 0 }}>
          The same permission lets PDF processing send a page's text to the model when the pattern extractor cannot read a table
          (for example a wide certificate with analytes in the header). Every cell the model returns must be found verbatim on
          the cited line or it is rejected; invalid output is retried once and then sent to manual review. Model-read rows are
          ordinary candidates and still need a second person's approval.
        </p>
        <div className="faint">
          Server provider: {llm?.configured ? <strong>{llm.provider} · {llm.model}</strong> : <>none configured (set <code>AJGEM_LLM_PROVIDER=anthropic</code> and credentials on the server)</>}
        </div>
        <label className="check">
          <input type="checkbox" checked={on} onChange={(e) => setAllow(e.target.checked)} />
          Allow this project's evidence excerpts to be sent to the configured provider
        </label>
        <label className="field">Processing basis (required to allow): provider retention, region, subprocessors and contract reference
          <textarea rows={3} value={text} onChange={(e) => setNotes(e.target.value)} placeholder="e.g. Anthropic commercial terms, no training on inputs; 30-day retention; US processing; approved by founder 2026-11-07" />
        </label>
        <ErrorNotice error={save.error} />
        <div><button className="primary" disabled={(allow === null && notes === null) || (on && !text.trim()) || save.pending} onClick={save.run}>Save as new configuration revision</button></div>
      </div>
    </section>
  );
}

const ofLimit = (used: number, limit: number | null | undefined) =>
  limit === null || limit === undefined ? `${used.toLocaleString()} (no limit)` : `${used.toLocaleString()} of ${limit.toLocaleString()}`;

function UsageAndBudget() {
  const { pid } = useProject();
  const u = useResource(() => api.get<UsageStatus>(`/projects/${pid}/usage`), [pid]);
  const [pages, setPages] = useState<string | null>(null);
  const [tokens, setTokens] = useState<string | null>(null);
  const lim = u.data?.limits.project ?? {};
  const pagesV = pages ?? (lim.monthly_pages ?? "").toString();
  const tokensV = tokens ?? (lim.monthly_model_tokens ?? "").toString();
  const save = useAction(async () => {
    const parse = (v: string) => (v.trim() === "" ? null : Number(v));
    await api.put(`/projects/${pid}/config`, { cost_policy: { monthly_pages: parse(pagesV), monthly_model_tokens: parse(tokensV) } });
    setPages(null); setTokens(null); u.reload();
  });
  if (!u.data) return u.error ? <ErrorNotice error={u.error} /> : <Loading />;
  const d = u.data;
  const tok = (r: { input_tokens: number; output_tokens: number }) => (r.input_tokens + r.output_tokens).toLocaleString();
  const rows: [string, string, string][] = [
    ["Pages processed", d.usage.pages.quantity.toLocaleString(), "PDF pages parsed (text layer or OCR)"],
    ["OCR pages", d.usage.ocr_pages.quantity.toLocaleString(), "Scanned pages read by OCR"],
    ["Model extraction", `${d.usage.model_extraction.events} calls · ${tok(d.usage.model_extraction)} tokens`, "Tables read with the language model"],
    ["Generated answers", `${d.usage.model_answers.events} calls · ${tok(d.usage.model_answers)} tokens`, "Narrative answers"],
    ["Embeddings", "not used", "Search is keyword-based"],
    ["Storage", bytes(d.storage_bytes), "Current stored originals"],
    ["Egress", bytes(d.usage.export_bytes.quantity), "Export downloads"],
  ];
  return (
    <section className="card">
      <div className="card-head"><h2>Usage and budget</h2><span className="faint">since {when(d.period_start)}</span></div>
      <div className="table-wrap" tabIndex={0}>
        <table className="data">
          <thead><tr><th>Category</th><th>This month</th><th>What it counts</th></tr></thead>
          <tbody>{rows.map(([a, b, c]) => <tr key={a}><td>{a}</td><td>{b}</td><td className="faint">{c}</td></tr>)}</tbody>
        </table>
      </div>
      <div className="card-pad stack">
        <div className="faint">
          Pages this month: project {ofLimit(d.used.project.monthly_pages, d.limits.project.monthly_pages)}; organisation {ofLimit(d.used.organisation.monthly_pages, d.limits.organisation.monthly_pages)}.
          Model tokens: project {ofLimit(d.used.project.monthly_model_tokens, d.limits.project.monthly_model_tokens)}; organisation {ofLimit(d.used.organisation.monthly_model_tokens, d.limits.organisation.monthly_model_tokens)}.
        </div>
        <p className="muted" style={{ margin: 0 }}>
          A PDF job whose page estimate exceeds a limit waits on the Jobs page for an administrator's approval with a reason.
          When model tokens run out, answers show search results only and unread tables go to manual review. The organisation budget is set by the operator.
        </p>
        <div className="form-grid">
          <label className="field">Project pages per month<input inputMode="numeric" value={pagesV} placeholder="no limit" onChange={(e) => setPages(e.target.value)} /></label>
          <label className="field">Project model tokens per month<input inputMode="numeric" value={tokensV} placeholder="no limit" onChange={(e) => setTokens(e.target.value)} /></label>
        </div>
        <ErrorNotice error={save.error} />
        <div><button className="primary" disabled={(pages === null && tokens === null) || save.pending} onClick={save.run}>Save quotas as new configuration revision</button></div>
      </div>
    </section>
  );
}

function Documents() {
  const { pid, reload } = useProject();
  const [tomb, setTomb] = useState<Tombstone | null>(null);
  const docs = useResource(() => api.get<DocumentList>(`/projects/${pid}/documents`), [pid, tomb]);
  const [deleting, setDeleting] = useState<string | null>(null);
  const versions = (docs.data?.documents ?? []).flatMap((d) => d.versions);
  return (
    <section className="card">
      <div className="card-head"><h2>Source documents and deletion</h2><span className="faint">Retention: kept until an administrator deletes them</span></div>
      {tomb && <div className="card-pad"><TombstoneSummary t={tomb} /></div>}
      {!versions.length ? <Empty>No documents.</Empty> : (
        <table className="data">
          <thead><tr><th>File</th><th>Version</th><th>Status</th><th>SHA-256</th><th>Uploaded</th><th /></tr></thead>
          <tbody>
            {versions.flatMap((v) => [
              <tr key={v.id}>
                <td>{v.filename}</td>
                <td>v{v.version_no}</td>
                <td><span className={`badge ${v.status === "deleted" ? "bad" : v.status === "active" ? "ok" : ""}`}>{v.status}</span></td>
                <td className="mono faint" title={v.sha256}>{v.sha256.slice(0, 12)}…</td>
                <td className="faint">{when(v.created_at)}</td>
                <td>{v.status !== "deleted" && <button className="small danger" onClick={() => { setTomb(null); setDeleting(deleting === v.id ? null : v.id); }}>Delete…</button>}</td>
              </tr>,
              deleting === v.id && (
                <tr key={`${v.id}-del`}><td colSpan={6}>
                  <DeleteForm url={`/versions/${v.id}`} what={`“${v.filename}” v${v.version_no}`} confirmText={v.filename}
                    onCancel={() => setDeleting(null)} onDone={(t) => { setDeleting(null); setTomb(t); reload(); }} />
                </td></tr>
              ),
            ])}
          </tbody>
        </table>
      )}
    </section>
  );
}

function DangerZone() {
  const { summary } = useProject();
  const nav = useNavigate();
  const [open, setOpen] = useState(false);
  const [tomb, setTomb] = useState<Tombstone | null>(null);
  return (
    <section className="card">
      <div className="card-head"><h2>Delete this project</h2></div>
      <div className="card-pad stack">
        {tomb ? (
          <>
            <TombstoneSummary t={tomb} />
            <div><button onClick={() => nav("/projects")}>Back to projects</button></div>
          </>
        ) : open ? (
          <DeleteForm url={`/projects/${summary.project.id}`} what={`project “${summary.project.name}”`} confirmText={summary.project.name}
            onCancel={() => setOpen(false)} onDone={setTomb} />
        ) : (
          <div className="row">
            <span className="muted">Removes every document, record, answer and export in this project and revokes all memberships.</span>
            <button className="danger" onClick={() => setOpen(true)}>Delete project…</button>
          </div>
        )}
      </div>
    </section>
  );
}

function Factors() {
  const { pid, roles } = useProject();
  const f = useResource(() => api.get<{ registry: string; factors: Factor[] }>(`/projects/${pid}/factors`), [pid]);
  const approve = useAction(async (x: Factor) => { await api.post(`/projects/${pid}/factor-approvals`, { element: x.element, oxide: x.oxide }); f.reload(); });
  return (
    <section className="card">
      <div className="card-head"><h2>Conversion factors</h2><span className="faint">{f.data?.registry}</span></div>
      {!f.data?.factors.length ? <Empty>No factors registered.</Empty> : (
        <table className="data">
          <thead><tr><th>Conversion</th><th className="num">Factor</th><th>Version</th><th>Provenance</th><th>Status</th><th /></tr></thead>
          <tbody>
            {f.data.factors.map((x) => (
              <tr key={x.version}>
                <td>{x.element} → {x.oxide}</td>
                <td className="num">{x.factor}</td>
                <td className="mono">{x.version}</td>
                <td className="faint">{x.provenance}</td>
                <td>{x.approved ? <span className="badge ok">approved</span> : <span className="badge warn">not approved</span>}</td>
                <td>{!x.approved && roles.has("reviewer") && <button className="small" onClick={() => { if (confirm(`Activate ${x.version} (×${x.factor}) for your whole organisation? This is recorded in the audit log.`)) approve.run(x); }}>Approve for organisation</button>}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      <div className="card-pad faint">Oxide equivalents are calculations from reported composition, not mineralogical measurements. The atomic-weight source for each factor must be verified independently before approval.</div>
      <div className="card-pad" style={{ paddingTop: 0 }}><ErrorNotice error={approve.error} /></div>
    </section>
  );
}

function Audit() {
  const { pid } = useProject();
  const a = useResource(() => api.get<{ events: AuditEv[] }>(`/projects/${pid}/audit`), [pid]);
  const members = useResource(() => api.get<{ members: Member[] }>(`/projects/${pid}/members`), [pid]);
  const [filter, setFilter] = useState("");
  const who = new Map(members.data?.members.map((m) => [m.user_id, m.email]));
  const evs = (a.data?.events ?? []).filter((e) => !filter || e.action.includes(filter)).slice().reverse();
  return (
    <section className="card">
      <div className="card-head">
        <h2>Audit log</h2>
        <input placeholder="filter action (e.g. record.approve)" value={filter} onChange={(e) => setFilter(e.target.value)} style={{ width: 260 }} />
      </div>
      {a.loading && !a.data ? <Loading /> : (
        <div className="table-wrap" tabIndex={0} style={{ maxHeight: 420 }}>
          <table className="data">
            <thead><tr><th>When</th><th>Actor</th><th>Action</th><th>Entity</th><th>Before → after</th><th>Detail</th></tr></thead>
            <tbody>
              {evs.map((e) => (
                <tr key={e.id}>
                  <td className="faint">{when(e.at)}</td>
                  <td>{who.get(e.actor) ?? e.actor.slice(0, 8)}</td>
                  <td className="mono">{e.action}</td>
                  <td className="mono faint">{e.entity_type} {e.entity_id.slice(0, 8)}</td>
                  <td className="mono faint">{e.before || e.after ? `${e.before?.slice(0, 8) ?? "∅"} → ${e.after?.slice(0, 8) ?? "∅"}` : ""}</td>
                  <td className="faint">{Object.keys(e.detail).length ? JSON.stringify(e.detail).slice(0, 120) : ""}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      <div className="card-pad faint">Events are append-only and cannot be edited through the application.</div>
    </section>
  );
}
