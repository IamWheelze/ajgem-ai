/** FR11 review workspace: original evidence beside candidate fields, corrections, second-person approval. */
import { useCallback, useEffect, useMemo, useState } from "react";
import { ApiError, api, fetchRecords, type RecordView } from "../lib/api";
import { FIELD_LABEL, FIELD_ORDER, qualified, recordTitle } from "../lib/format";
import { useAction, useResource } from "../lib/hooks";
import { useSession } from "../lib/session";
import { useProject } from "../components/Layout";
import { AnchorCard, evidenceItems } from "../components/SourceEvidence";
import { Empty, ErrorNotice, IssueList, Loading, StatusBadge } from "../components/ui";

type KindFilter = "all" | "assay" | "sample" | "certificate";

export default function Review() {
  const { pid, can, reload: reloadSummary } = useProject();
  const { me } = useSession();
  const [kind, setKind] = useState<KindFilter>("all");
  const [onlyOthers, setOnlyOthers] = useState(true);
  const queue = useResource(() => fetchRecords(pid), [pid]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [checked, setChecked] = useState<Set<string>>(new Set());

  const items = useMemo(() => {
    const pending = (queue.data ?? []).filter((r) => r.revision_state === "needs_review");
    return pending.filter((r) => (kind === "all" || r.kind === kind) && (!onlyOthers || r.created_by !== me?.id));
  }, [queue.data, kind, onlyOthers, me?.id]);

  useEffect(() => {
    if (!items.find((r) => r.id === selectedId)) setSelectedId(items[0]?.id ?? null);
  }, [items, selectedId]);

  const move = useCallback((d: number) => {
    const i = items.findIndex((r) => r.id === selectedId);
    const n = items[Math.min(items.length - 1, Math.max(0, i + d))];
    if (n) setSelectedId(n.id);
  }, [items, selectedId]);

  useEffect(() => {
    const k = (e: KeyboardEvent) => {
      const t = e.target as HTMLElement;
      if (["INPUT", "TEXTAREA", "SELECT"].includes(t.tagName)) return;
      if (e.key === "j") move(1);
      if (e.key === "k") move(-1);
    };
    window.addEventListener("keydown", k);
    return () => window.removeEventListener("keydown", k);
  }, [move]);

  const refresh = () => { queue.reload(); reloadSummary(); };
  const [flash, setFlash] = useState<{ title: string; diff: Record<string, { before: unknown; after: unknown }> } | null>(null);
  const selected = items.find((r) => r.id === selectedId) ?? null;

  if (!can("drafts")) return <div className="page"><ErrorNotice error={new Error("Your role can only see approved records.")} title="Review not available" /></div>;
  return (
    <div className="page full">
      <div className="review">
        <aside className="queue" aria-label="Review queue">
          <div className="card-pad stack" style={{ borderBottom: "1px solid var(--border)" }}>
            <div className="row between"><h2>Review queue</h2><span className="badge warn">{items.length}</span></div>
            <div className="chips" role="group" aria-label="Kind">
              {(["all", "assay", "sample", "certificate"] as KindFilter[]).map((k) => (
                <button key={k} className={`chip ${kind === k ? "on" : ""}`} onClick={() => setKind(k)} aria-pressed={kind === k}>{k}</button>
              ))}
            </div>
            <label className="check faint"><input type="checkbox" checked={onlyOthers} onChange={(e) => setOnlyOthers(e.target.checked)} /> Hide revisions I authored</label>
            <span className="faint"><span className="kbd">j</span> / <span className="kbd">k</span> next / previous</span>
          </div>
          {flash && (
            <div className="card-pad" style={{ borderBottom: "1px solid var(--border)" }}>
              <div className="notice ok"><div><strong>Correction saved: {flash.title}</strong>
                <div className="diff">{Object.entries(flash.diff).map(([k, v]) => <div key={k}>{FIELD_LABEL[k] ?? k}: <del>{String(v.before ?? "∅")}</del> → <ins>{String(v.after ?? "∅")}</ins></div>)}</div>
                New revision awaits another reviewer. <button className="link" onClick={() => setFlash(null)}>Dismiss</button></div></div>
            </div>
          )}
          {can("review") && <BulkBar items={items} checked={checked} setChecked={setChecked} onDone={refresh} />}
          {queue.loading && !queue.data ? <Loading /> : items.length === 0 ? <Empty>Nothing waiting for you.</Empty> : items.map((r) => {
            const crit = r.issues?.filter((i) => i.severity === "critical").length ?? 0;
            const warn = r.issues?.filter((i) => i.severity === "warning").length ?? 0;
            return (
              <div key={r.id} className="row" style={{ gap: 0, alignItems: "stretch" }}>
                {can("review") && (
                  <label style={{ padding: "12px 0 0 10px" }} aria-label={`Select ${recordTitle(r)}`}>
                    <input type="checkbox" checked={checked.has(r.id)} onChange={(e) => { const s = new Set(checked); if (e.target.checked) s.add(r.id); else s.delete(r.id); setChecked(s); }} />
                  </label>
                )}
                <button className={`queue-item ${r.id === selectedId ? "active" : ""}`} onClick={() => setSelectedId(r.id)} style={{ flex: 1 }}>
                  <span className="t">{recordTitle(r)}</span>
                  <span className="row" style={{ gap: 4 }}>
                    <span className="badge">{r.kind}</span>
                    {r.kind === "assay" && <span className="mono faint">{qualified(String(r.data?.comparator ?? "EQ"), r.data?.value as string, r.data?.unit as string)}</span>}
                    {crit > 0 && <span className="badge bad">{crit} critical</span>}
                    {warn > 0 && <span className="badge warn">{warn} warn</span>}
                    {r.stale && <span className="badge warn">stale</span>}
                    {r.created_by === me?.id && <span className="badge info">yours</span>}
                  </span>
                </button>
              </div>
            );
          })}
        </aside>
        {selected ? <ReviewItem key={`${selected.id}:${selected.current_revision_no}`} record={selected} onChanged={refresh} onSaved={(diff) => setFlash({ title: recordTitle(selected), diff })} /> : <div className="pane"><Empty>Select a record.</Empty></div>}
      </div>
    </div>
  );
}

function BulkBar({ items, checked, setChecked, onDone }: { items: RecordView[]; checked: Set<string>; setChecked: (s: Set<string>) => void; onDone: () => void }) {
  const { pid } = useProject();
  const { me } = useSession();
  const sel = items.filter((r) => checked.has(r.id));
  const blocked = sel.filter((r) => r.issues?.some((i) => i.severity === "critical") || r.created_by === me?.id);
  const warnCodes = [...new Set(sel.flatMap((r) => r.issues?.filter((i) => i.severity === "warning").map((i) => i.code) ?? []))];
  const [ack, setAck] = useState(false);
  const [res, setRes] = useState<{ failed: { record_id: string; error: { code: string; message: string } }[] } | null>(null);
  const run = useAction(async () => {
    const ok = sel.filter((r) => !blocked.includes(r));
    const r = await api.post<{ failed: { record_id: string; error: { code: string; message: string } }[] }>(`/projects/${pid}/reviews/bulk`, {
      items: ok.map((x) => ({ record_id: x.id, expected_revision_no: x.current_revision_no })), decision: "approve", acknowledge_warnings: ack ? warnCodes : [],
    });
    setRes(r);
    setChecked(new Set());
    onDone();
  });
  const homogeneous = new Set(items.map((r) => `${r.kind}`)).size;
  return (
    <div className="card-pad stack" style={{ borderBottom: "1px solid var(--border)", background: "var(--surface-2)" }}>
      <div className="row between">
        <span className="faint">{sel.length} selected</span>
        <div className="row">
          <button className="small" onClick={() => setChecked(new Set(items.filter((r) => !r.issues?.some((i) => i.severity === "critical") && r.created_by !== me?.id).map((r) => r.id)))}>Select approvable</button>
          {sel.length > 0 && <button className="small ghost" onClick={() => setChecked(new Set())}>Clear</button>}
        </div>
      </div>
      {sel.length > 0 && (
        <>
          {homogeneous > 1 && <span className="faint">Tip: filter by kind so a batch is homogeneous.</span>}
          {warnCodes.length > 0 && (
            <label className="check faint"><input type="checkbox" checked={ack} onChange={(e) => setAck(e.target.checked)} /> Acknowledge for all selected: {warnCodes.join(", ")}</label>
          )}
          {blocked.length > 0 && <span className="faint">{blocked.length} will be skipped (critical issues or authored by you).</span>}
          <button className="primary small" disabled={run.pending || sel.length === blocked.length || (warnCodes.length > 0 && !ack)} onClick={run.run}>Approve {sel.length - blocked.length}</button>
        </>
      )}
      <ErrorNotice error={run.error} />
      {res && res.failed.length > 0 && <div className="notice warn"><div><strong>{res.failed.length} not approved</strong>{res.failed.slice(0, 5).map((f) => <div key={f.record_id} className="faint">{f.record_id.slice(0, 8)}: {f.error.code}</div>)}</div></div>}
    </div>
  );
}

function ReviewItem({ record, onChanged, onSaved }: { record: RecordView; onChanged: () => void; onSaved: (d: Record<string, { before: unknown; after: unknown }>) => void }) {
  const { can } = useProject();
  const { me } = useSession();
  const fields = FIELD_ORDER[record.kind];
  const original = record.data ?? {};
  const [draft, setDraft] = useState<Record<string, string>>(() => Object.fromEntries(fields.map((f) => [f, original[f] === null || original[f] === undefined ? "" : String(original[f])])));
  const [reason, setReason] = useState("");
  const [focus, setFocus] = useState<string | null>(null);
  const [ack, setAck] = useState<Set<string>>(new Set());
  const revisions = useResource(() => api.get<{ revisions: { revision_no: number; state: string; created_by: string; decided_by: string | null; reason: string | null }[] }>(`/records/${record.id}/revisions`), [record.id, record.current_revision_no]);

  const changes = Object.fromEntries(
    fields.filter((f) => draft[f] !== (original[f] === null || original[f] === undefined ? "" : String(original[f]))).map((f) => [f, f === "crs_confirmed" ? draft[f] === "true" : draft[f] === "" ? null : draft[f]]),
  );
  const dirty = Object.keys(changes).length > 0;
  const issues = record.issues ?? [];
  const critical = issues.filter((i) => i.severity === "critical");
  const warnings = issues.filter((i) => i.severity === "warning").map((i) => i.code);
  const mine = record.created_by === me?.id;
  const flagged = new Set(issues.filter((i) => i.severity !== "info").map((i) => i.field));

  const save = useAction(async () => {
    const r = await api.post<{ diff: Record<string, { before: unknown; after: unknown }> }>(`/records/${record.id}/edits`, { expected_revision_no: record.current_revision_no, changes, reason });
    onSaved(r.diff);
    onChanged();
  });
  const decide = useAction(async (decision: "approve" | "reject") => {
    await api.post(`/records/${record.id}/review`, { expected_revision_no: record.current_revision_no, decision, reason: reason || undefined, acknowledge_warnings: [...ack] });
    onChanged();
  });
  const err = save.error ?? decide.error;
  const conflict = err instanceof ApiError && err.code === "REVISION_CONFLICT";

  const ev = evidenceItems(record.field_anchors, record.anchor_ids);
  const focused = focus ? ev.filter((e) => e.field?.split(", ").includes(focus)) : [];
  const shown = focused.length ? [...focused, ...ev.filter((e) => !focused.includes(e))] : ev;

  return (
    <>
      <section className="pane" aria-label="Source evidence">
        <div className="row between">
          <h2>Original source</h2>
          {record.stale && <span className="badge warn">source superseded — re-check</span>}
        </div>
        {focus && <div className="faint">Showing evidence for <strong>{FIELD_LABEL[focus] ?? focus}</strong> first.</div>}
        {shown.length === 0 && <div className="notice bad"><div><strong>No source anchors</strong>This candidate has no linked evidence; do not approve without checking the original.</div></div>}
        {shown.map((e, n) => <AnchorCard key={e.anchorId} anchorId={e.anchorId} field={e.field} showOriginal={n === 0 && record.extraction_method?.startsWith("pattern") === true} />)}
      </section>
      <section className="pane" aria-label="Candidate">
        <div className="row between">
          <div>
            <h2>{recordTitle(record)}</h2>
            <div className="faint">{record.kind} · revision {record.current_revision_no} · {record.extraction_method} · extraction uncertainty {record.extraction_uncertainty}</div>
          </div>
          <StatusBadge status={record.status} />
        </div>
        {record.approved_revision_no && <div className="notice info"><div>Revision {record.approved_revision_no} is approved and remains in use until this correction is approved.</div></div>}
        {mine && <div className="notice info"><div><strong>You authored this revision</strong>A second person must approve it.</div></div>}

        <div role="group" aria-label="Fields">
          {fields.map((f) => {
            const orig = original[f] === null || original[f] === undefined ? "" : String(original[f]);
            return (
              <div key={f} className={`fieldrow ${draft[f] !== orig ? "changed" : ""} ${flagged.has(f) ? "flag" : ""}`}>
                <label className="lbl" htmlFor={`f-${f}`}>{FIELD_LABEL[f] ?? f}</label>
                {f === "crs_confirmed" ? (
                  <select id={`f-${f}`} value={draft[f] || "false"} onFocus={() => setFocus(f)} disabled={!can("edit")} onChange={(e) => setDraft({ ...draft, [f]: e.target.value })}><option value="false">no</option><option value="true">yes</option></select>
                ) : f === "axis_order" ? (
                  <select id={`f-${f}`} value={draft[f]} onFocus={() => setFocus(f)} disabled={!can("edit")} onChange={(e) => setDraft({ ...draft, [f]: e.target.value })}><option value="">unconfirmed</option><option value="xy">xy</option><option value="yx">yx</option></select>
                ) : (
                  <input id={`f-${f}`} className={["result_raw", "unit", "raw_x", "raw_y"].includes(f) ? "mono" : ""} value={draft[f]} onFocus={() => setFocus(f)} disabled={!can("edit")} onChange={(e) => setDraft({ ...draft, [f]: e.target.value })} />
                )}
                <span className="faint mono" title="Stored as">{draft[f] !== orig ? `was ${orig || "∅"}` : ""}</span>
              </div>
            );
          })}
          {record.kind === "assay" && (
            <div className="fieldrow"><span className="lbl">Parsed qualifier</span><span className="mono">{qualified(String(original.comparator ?? ""), original.value as string, original.unit as string)}</span><span className="faint">derived, not editable</span></div>
          )}
          {record.kind === "sample" && record.geometry && <div className="fieldrow"><span className="lbl">Approved geometry</span><span className="mono">{record.geometry.coordinates.map((c) => c.toFixed(6)).join(", ")}</span><span /></div>}
        </div>

        <h3>Validation</h3>
        <IssueList issues={issues} ack={ack} onAck={can("review") && !dirty ? (c, on) => { const s = new Set(ack); if (on) s.add(c); else s.delete(c); setAck(s); } : undefined} />

        <label className="field">Reason {dirty ? "(required for a correction)" : "(required to reject)"}
          <textarea rows={2} value={reason} onChange={(e) => setReason(e.target.value)} placeholder="e.g. Decimal point lost in transcription; certificate page 8 shows 17.1" />
        </label>

        {conflict ? (
          <div className="notice warn"><div><strong>Someone else changed this record</strong>Reload to see the current revision before deciding. <button className="link" onClick={onChanged}>Reload</button></div></div>
        ) : <ErrorNotice error={err} />}

        <div className="row">
          {can("edit") && <button disabled={!dirty || !reason.trim() || save.pending} onClick={save.run}>Save correction</button>}
          {dirty && <button className="ghost" onClick={() => setDraft(Object.fromEntries(fields.map((f) => [f, original[f] == null ? "" : String(original[f])])))}>Discard changes</button>}
          <span className="spacer" />
          {can("review") && (
            <>
              <button className="danger" disabled={dirty || !reason.trim() || mine || decide.pending} onClick={() => decide.run("reject")}>Reject</button>
              <button className="primary" disabled={dirty || mine || critical.length > 0 || warnings.some((w) => !ack.has(w)) || decide.pending} onClick={() => decide.run("approve")}
                title={critical.length ? "Critical issues block approval" : mine ? "Self-approval is blocked" : undefined}>Approve revision {record.current_revision_no}</button>
            </>
          )}
        </div>
        {critical.length > 0 && can("review") && <div className="faint">Approval is blocked until critical issues are corrected.</div>}

        {revisions.data && revisions.data.revisions.length > 1 && (
          <details>
            <summary>Revision history ({revisions.data.revisions.length})</summary>
            <table className="data" style={{ marginTop: 8 }}>
              <thead><tr><th>Rev</th><th>State</th><th>Reason</th></tr></thead>
              <tbody>{revisions.data.revisions.map((r) => <tr key={r.revision_no}><td>{r.revision_no}</td><td><StatusBadge status={r.state} /></td><td className="faint">{r.reason ?? ""}</td></tr>)}</tbody>
            </table>
          </details>
        )}
      </section>
    </>
  );
}
