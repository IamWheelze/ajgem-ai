/** FR12 conflicts and versions: compare competing results, select a governing one with rationale. */
import { useState } from "react";
import { api, type ConflictView } from "../lib/api";
import { useAction, useResource } from "../lib/hooks";
import { useProject } from "../components/Layout";
import { SourceDrawer, type EvidenceItem } from "../components/SourceEvidence";
import { Empty, ErrorNotice, Loading, StatusBadge } from "../components/ui";

export default function Conflicts() {
  const { pid, reload } = useProject();
  const list = useResource(() => api.get<{ conflicts: ConflictView[] }>(`/projects/${pid}/conflicts`), [pid]);
  const [filter, setFilter] = useState<"open" | "all">("open");
  const [drawer, setDrawer] = useState<{ title: string; items: EvidenceItem[] } | null>(null);
  const groups = (list.data?.conflicts ?? []).filter((g) => filter === "all" || g.status === "open");
  return (
    <div className="page">
      <div className="page-head">
        <div>
          <h1>Conflicts & versions</h1>
          <p>Results that compete for the same sample, analyte and basis. Nothing is merged or replaced automatically — a revised certificate is not authoritative just because it arrived later. Unresolved groups are excluded from calculations.</p>
        </div>
        <div className="chips">
          <button className={`chip ${filter === "open" ? "on" : ""}`} onClick={() => setFilter("open")}>Open · {list.data?.conflicts.filter((g) => g.status === "open").length ?? 0}</button>
          <button className={`chip ${filter === "all" ? "on" : ""}`} onClick={() => setFilter("all")}>All · {list.data?.conflicts.length ?? 0}</button>
        </div>
      </div>
      {list.loading && !list.data ? <Loading /> : list.error ? <ErrorNotice error={list.error} /> : groups.length === 0 ? <div className="card"><Empty>No {filter === "open" ? "open " : ""}conflicts.</Empty></div> : (
        groups.map((g) => <Group key={`${g.id}:${g.version}`} g={g} onDone={() => { list.reload(); reload(); }} onEvidence={(t, ids) => setDrawer({ title: t, items: ids.map((anchorId) => ({ anchorId })) })} />)
      )}
      {drawer && <SourceDrawer title={drawer.title} items={drawer.items} onClose={() => setDrawer(null)} />}
    </div>
  );
}

function Group({ g, onDone, onEvidence }: { g: ConflictView; onDone: () => void; onEvidence: (t: string, ids: string[]) => void }) {
  const { can } = useProject();
  const [gov, setGov] = useState<string>(g.governing_record_id ?? "");
  const [reason, setReason] = useState("");
  const resolve = useAction(async () => {
    await api.post(`/conflicts/${g.id}/resolve`, { governing_record_id: gov, reason, expected_version: g.version });
    onDone();
  });
  const [sample, analyte, basis] = g.key.split("|");
  const sid = sample.split("::")[1] ?? sample;
  return (
    <section className="card">
      <div className="card-head">
        <div>
          <h2 className="mono">{sid} · {analyte} · {basis}</h2>
          {g.reason && <div className="faint">Rationale: {g.reason}</div>}
        </div>
        <StatusBadge status={g.status} label={g.status === "open" && g.governing_record_id ? "re-opened (previous selection still governs)" : undefined} />
      </div>
      <div className="table-wrap" tabIndex={0}>
        <table className="data">
          <thead><tr>{can("review") && <th>Governing</th>}<th>Certificate</th><th className="num">Result</th><th>Record status</th><th>Revision</th><th>Evidence</th></tr></thead>
          <tbody>
            {g.members.map((m) => (
              <tr key={m.record_id} className={m.governing ? "selected" : ""}>
                {can("review") && (
                  <td>
                    <input type="radio" name={`gov-${g.id}`} aria-label={`Select ${m.certificate_number} as governing`} checked={gov === m.record_id} disabled={m.status !== "approved"} onChange={() => setGov(m.record_id)} />
                    {m.status !== "approved" && <span className="faint"> approve first</span>}
                  </td>
                )}
                <td className="mono">{m.certificate_number ?? "—"}{m.governing && <> <span className="badge ok">governing</span></>}</td>
                <td className="num">{m.result_raw} {m.unit}</td>
                <td><StatusBadge status={m.status} /></td>
                <td className="faint">r{m.revision_no}</td>
                <td><button className="link small" onClick={() => onEvidence(`Evidence · ${m.certificate_number}`, m.anchor_ids)}>{m.anchor_ids.length} sources</button></td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {can("review") && g.status !== "consistent" && (
        <div className="card-pad stack">
          <label className="field">Rationale for the governing result (recorded in the audit log)
            <textarea rows={2} value={reason} onChange={(e) => setReason(e.target.value)} placeholder="e.g. Laboratory reissued certificate after re-assay of pulp; R1 governs" />
          </label>
          <ErrorNotice error={resolve.error} />
          <div className="row">
            <button className="primary" disabled={!gov || !reason.trim() || resolve.pending || gov === g.governing_record_id && g.status === "resolved"} onClick={resolve.run}>Select governing result</button>
            <span className="faint">Excluded alternatives remain visible and auditable. Cached answers using this sample become stale.</span>
          </div>
        </div>
      )}
    </section>
  );
}
