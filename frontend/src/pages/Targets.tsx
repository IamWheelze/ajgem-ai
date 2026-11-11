/** FR18 targets: linked reviewed evidence (supporting / contrary / context) against a named checklist version. */
import { useState } from "react";
import { api, type RecordView } from "../lib/api";
import { qualified } from "../lib/format";
import { useAction, useResource } from "../lib/hooks";
import { useProject } from "../components/Layout";
import { SourceDrawer, evidenceItems, type EvidenceItem } from "../components/SourceEvidence";
import { Empty, ErrorNotice, Loading, StatusBadge } from "../components/ui";

type Stance = "supporting" | "contrary" | "context";
const STANCES: Stance[] = ["supporting", "contrary", "context"];
const STANCE_LABEL: Record<Stance, string> = { supporting: "Supporting", contrary: "Contrary", context: "Context" };

interface ChecklistItem { id: string; label: string; description?: string }
interface Checklist { id: string; name: string; version: number; items: ChecklistItem[] }
interface TargetRow { id: string; name: string; status: string; checklist_id: string | null; evidence_count: number }
interface Evidence {
  evidence_id: string; record_id: string; revision_id: string; kind: string | null; sample_id?: string; analyte?: string;
  result_raw?: string; unit?: string; comparator?: string; certificate_number?: string; anchor_ids: string[]; note: string;
  checklist_item_ids: string[]; linked_at: string; evidence_changed: boolean;
}
interface Summary {
  target: { id: string; name: string; description: string; status: string; geometry: unknown };
  checklist: { id: string; name: string; version: number } | null;
  evidence: Record<Stance, Evidence[]>;
  counts: Record<Stance, number>;
  checklist_items: (ChecklistItem & { status: "addressed" | "missing"; supporting: number; contrary: number; evidence_ids: string[] })[];
  missing_items: ChecklistItem[];
  empty: boolean;
  message: string | null;
  notice: string;
}

export default function Targets() {
  const { pid, roles } = useProject();
  const canEdit = roles.has("analyst") || roles.has("reviewer");
  const canChecklist = roles.has("admin") || roles.has("reviewer");
  const list = useResource(() => api.get<{ targets: TargetRow[] }>(`/projects/${pid}/targets`), [pid]);
  const checklists = useResource(() => api.get<{ checklists: Checklist[] }>(`/projects/${pid}/checklists`), [pid]);
  const [open, setOpen] = useState<string | null>(null);
  const [name, setName] = useState("");
  const [desc, setDesc] = useState("");
  const [cl, setCl] = useState("");
  const create = useAction(async () => {
    const s = await api.post<Summary>(`/projects/${pid}/targets`, { name, description: desc, checklist_id: cl || null });
    setName(""); setDesc(""); list.reload(); setOpen(s.target.id);
  });
  return (
    <div className="page">
      <div className="page-head">
        <div>
          <h1>Targets</h1>
          <p>Named targets with the reviewed evidence people have linked to them, compared with a checklist. A target summary is a record of evidence, not an assessment: it never ranks targets or declares a resource.</p>
        </div>
      </div>
      <div className="grid-2">
        <section className="card">
          <div className="card-head"><h2>Targets</h2></div>
          {list.loading && !list.data ? <Loading /> : list.error ? <ErrorNotice error={list.error} /> : !list.data?.targets.length ? <Empty>No targets yet.</Empty> : (
            <table className="data">
              <thead><tr><th>Name</th><th className="num">Linked evidence</th><th>Status</th></tr></thead>
              <tbody>
                {list.data.targets.map((t) => (
                  <tr key={t.id} className={`clickable ${open === t.id ? "selected" : ""}`} tabIndex={0}
                      onClick={() => setOpen(t.id)} onKeyDown={(e) => e.key === "Enter" && setOpen(t.id)}>
                    <td>{t.name}</td><td className="num">{t.evidence_count}</td><td><StatusBadge status={t.status} /></td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
          {canEdit && (
            <form className="card-pad stack" onSubmit={(e) => { e.preventDefault(); if (name.trim()) create.run(); }}>
              <label className="field">New target name<input value={name} onChange={(e) => setName(e.target.value)} placeholder="e.g. Dyke D7" /></label>
              <label className="field">Description<input value={desc} onChange={(e) => setDesc(e.target.value)} placeholder="What is being assessed" /></label>
              <label className="field">Checklist
                <select aria-label="Checklist" value={cl} onChange={(e) => setCl(e.target.value)}>
                  <option value="">None</option>
                  {checklists.data?.checklists.map((c) => <option key={c.id} value={c.id}>{c.name} v{c.version}</option>)}
                </select>
              </label>
              <ErrorNotice error={create.error} />
              <div><button className="primary" disabled={!name.trim() || create.pending}>Create target</button></div>
            </form>
          )}
        </section>
        {canChecklist ? <ChecklistEditor checklists={checklists.data?.checklists ?? []} onSaved={checklists.reload} /> : <ChecklistList checklists={checklists.data?.checklists ?? []} />}
      </div>
      {open && <TargetDetail key={open} id={open} canEdit={canEdit} onChange={list.reload} />}
    </div>
  );
}

function ChecklistList({ checklists }: { checklists: Checklist[] }) {
  return (
    <section className="card">
      <div className="card-head"><h2>Checklists</h2></div>
      {!checklists.length ? <Empty>No checklists yet.</Empty> : (
        <ul className="card-pad">{checklists.map((c) => <li key={c.id}>{c.name} v{c.version} · {c.items.length} items</li>)}</ul>
      )}
    </section>
  );
}

function ChecklistEditor({ checklists, onSaved }: { checklists: Checklist[]; onSaved: () => void }) {
  const { pid } = useProject();
  const [name, setName] = useState("");
  const [text, setText] = useState("");
  const save = useAction(async () => {
    const items = text.split("\n").map((l) => l.trim()).filter(Boolean).map((l) => {
      const [id, ...rest] = l.split(":");
      return { id: id.trim().toLowerCase(), label: rest.join(":").trim() || id.trim() };
    });
    await api.post(`/projects/${pid}/checklists`, { name, items });
    setText(""); onSaved();
  });
  const load = (c: Checklist) => { setName(c.name); setText(c.items.map((i) => `${i.id}: ${i.label}`).join("\n")); };
  return (
    <section className="card">
      <div className="card-head"><h2>Checklists</h2><span className="faint">Saving creates a new version; earlier versions never change.</span></div>
      <div className="card-pad stack">
        {checklists.length > 0 && (
          <div className="chips">{checklists.map((c) => <button key={c.id} type="button" className="chip" onClick={() => load(c)}>{c.name} v{c.version}</button>)}</div>
        )}
        <label className="field">Checklist name<input value={name} onChange={(e) => setName(e.target.value)} placeholder="e.g. Pegmatite Li screening" /></label>
        <label className="field">Items, one per line as <code>id: label</code>
          <textarea rows={5} value={text} onChange={(e) => setText(e.target.value)} placeholder={"host-rock: Host pegmatite mapped\nli-assay: Li assay above cut-off reported"} />
        </label>
        <ErrorNotice error={save.error} />
        <div><button className="primary" disabled={!name.trim() || !text.trim() || save.pending} onClick={save.run}>Save checklist version</button></div>
      </div>
    </section>
  );
}

function TargetDetail({ id, canEdit, onChange }: { id: string; canEdit: boolean; onChange: () => void }) {
  const { pid } = useProject();
  const t = useResource(() => api.get<Summary>(`/targets/${id}`), [id]);
  const [drawer, setDrawer] = useState<{ title: string; items: EvidenceItem[] } | null>(null);
  const [sample, setSample] = useState("");
  const [found, setFound] = useState<RecordView[] | null>(null);
  const [pick, setPick] = useState<{ record: RecordView; stance: Stance; note: string; items: Set<string> } | null>(null);
  const [err, setErr] = useState<unknown>();
  if (!t.data) return t.error ? <ErrorNotice error={t.error} /> : <Loading />;
  const s = t.data;
  const find = async () => {
    setErr(undefined);
    const hits = await api.get<{ exact: { record_id: string; kind: string; status: string }[] }>(`/projects/${pid}/search`, { q: sample });
    const recs = await Promise.all(hits.exact.filter((h) => h.status === "approved").map((h) => api.get<RecordView>(`/records/${h.record_id}`)));
    setFound(recs);
  };
  const link = async () => {
    if (!pick) return;
    try {
      t.setData(await api.post<Summary>(`/targets/${id}/evidence`, { record_id: pick.record.id, stance: pick.stance, note: pick.note, checklist_item_ids: [...pick.items] }));
      setPick(null); setFound(null); setSample(""); onChange();
    } catch (e) { setErr(e); }
  };
  const unlink = async (eid: string) => {
    const reason = window.prompt("Reason for removing this link (recorded in the audit log)");
    if (!reason) return;
    try { t.setData(await api.del<Summary>(`/targets/${id}/evidence/${eid}`, { reason })); onChange(); } catch (e) { setErr(e); }
  };
  return (
    <section className="card">
      <div className="card-head">
        <div><h2>{s.target.name}</h2>{s.target.description && <span className="faint">{s.target.description}</span>}</div>
        <span className="faint">{s.checklist ? `Checklist: ${s.checklist.name} v${s.checklist.version}` : "No checklist"}</span>
      </div>
      <div className="card-pad stack">
        <div className="notice"><div>{s.notice}</div></div>
        {s.empty && <Empty>{s.message}</Empty>}
        {s.checklist_items.length > 0 && (
          <div>
            <h3>Checklist completeness</h3>
            <table className="data">
              <thead><tr><th>Item</th><th>Status</th><th className="num">Supporting</th><th className="num">Contrary</th></tr></thead>
              <tbody>{s.checklist_items.map((i) => (
                <tr key={i.id}><td>{i.label} <span className="faint mono">{i.id}</span></td>
                  <td><span className={`badge ${i.status === "missing" ? "warn" : "ok"}`}>{i.status === "missing" ? "missing evidence" : "evidence linked"}</span></td>
                  <td className="num">{i.supporting}</td><td className="num">{i.contrary}</td></tr>
              ))}</tbody>
            </table>
          </div>
        )}
        {STANCES.map((st) => (
          <div key={st}>
            <h3>{STANCE_LABEL[st]} · {s.counts[st]}</h3>
            {s.evidence[st].length === 0 ? <div className="faint">None linked.</div> : (
              <table className="data">
                <thead><tr><th>Record</th><th className="num">Reported</th><th>Certificate</th><th>Checklist items</th><th>Note</th><th /></tr></thead>
                <tbody>{s.evidence[st].map((e) => (
                  <tr key={e.evidence_id}>
                    <td>{e.sample_id ?? e.record_id.slice(0, 8)}{e.analyte ? ` · ${e.analyte}` : ""}{e.evidence_changed && <span className="badge warn" title="The record was re-approved or its source changed after this link was made; the link still shows the revision it was made against">changed since linked</span>}</td>
                    <td className="num">{e.result_raw ? `${qualified(e.comparator ?? "EQ", e.result_raw.replace(/^[<>]=?/, ""))} ${e.unit ?? ""}` : "—"}</td>
                    <td>{e.certificate_number ?? "—"}</td>
                    <td>{e.checklist_item_ids.join(", ") || "—"}</td>
                    <td>{e.note || "—"}</td>
                    <td className="row">
                      <button className="small" onClick={() => setDrawer({ title: e.sample_id ?? "Evidence", items: evidenceItems(undefined, e.anchor_ids) })}>Evidence</button>
                      {canEdit && <button className="small" onClick={() => unlink(e.evidence_id)}>Remove</button>}
                    </td>
                  </tr>
                ))}</tbody>
              </table>
            )}
          </div>
        ))}
        {canEdit && (
          <div className="stack">
            <h3>Link reviewed evidence</h3>
            <form className="row" onSubmit={(e) => { e.preventDefault(); if (sample.trim()) find(); }}>
              <label className="field" style={{ flex: 1 }}>Sample ID or certificate number<input value={sample} onChange={(e) => setSample(e.target.value)} placeholder="KW-001" /></label>
              <button style={{ alignSelf: "flex-end" }} disabled={!sample.trim()}>Find approved records</button>
            </form>
            {found && (found.length === 0 ? <div className="faint">No approved records match. Only reviewed records can be linked.</div> : (
              <div className="chips">{found.map((r) => (
                <button key={r.id} type="button" className={`chip ${pick?.record.id === r.id ? "on" : ""}`}
                        onClick={() => setPick({ record: r, stance: "supporting", note: "", items: new Set() })}>
                  {r.kind} · {String(r.data?.sample_id ?? "")}{r.data?.analyte ? ` · ${String(r.data.analyte)} ${String(r.data.result_raw ?? "")} ${String(r.data.unit ?? "")}` : ""}
                </button>
              ))}</div>
            ))}
            {pick && (
              <div className="stack">
                <div className="chips" role="group" aria-label="Stance">
                  {STANCES.map((st) => <button key={st} type="button" className={`chip ${pick.stance === st ? "on" : ""}`} aria-pressed={pick.stance === st} onClick={() => setPick({ ...pick, stance: st })}>{STANCE_LABEL[st]}</button>)}
                </div>
                {s.checklist_items.length > 0 && (
                  <fieldset className="stack"><legend>Checklist items this evidence addresses</legend>
                    {s.checklist_items.map((i) => (
                      <label key={i.id} className="check"><input type="checkbox" checked={pick.items.has(i.id)} onChange={(e) => {
                        const n = new Set(pick.items); if (e.target.checked) n.add(i.id); else n.delete(i.id); setPick({ ...pick, items: n });
                      }} /> {i.label}</label>
                    ))}
                  </fieldset>
                )}
                <label className="field">Note<input value={pick.note} onChange={(e) => setPick({ ...pick, note: e.target.value })} placeholder="Why this record bears on the target" /></label>
                <div><button className="primary" onClick={link}>Link as {STANCE_LABEL[pick.stance].toLowerCase()} evidence</button></div>
              </div>
            )}
          </div>
        )}
        <ErrorNotice error={err} />
      </div>
      {drawer && <SourceDrawer title={drawer.title} items={drawer.items} onClose={() => setDrawer(null)} />}
    </section>
  );
}
