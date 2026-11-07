import { Link } from "react-router-dom";
import { api, type JobView } from "../lib/api";
import { useResource } from "../lib/hooks";
import { when } from "../lib/format";
import { useProject } from "../components/Layout";
import { Empty, StatusBadge, Tile } from "../components/ui";

export default function Dashboard() {
  const { pid, summary: s, can } = useProject();
  const jobs = useResource(() => api.get<{ jobs: JobView[] }>(`/projects/${pid}/jobs`), [pid]);
  const answers = useResource(() => api.get<{ answers: { id: string; question: string; state: string; stale_reason: string | null; created_at: string }[] }>(`/projects/${pid}/answers`), [pid]);
  const rec = (k: string, st: string) => s.records[k]?.[st] ?? 0;
  const pending = Object.values(s.records).reduce((n, k) => n + (k.needs_review ?? 0), 0);
  const approved = Object.values(s.records).reduce((n, k) => n + (k.approved ?? 0), 0);
  const base = `/p/${pid}`;
  return (
    <div className="page">
      <div className="page-head">
        <div>
          <h1>{s.project.name}</h1>
          <p>Project evidence status. Extracted data only becomes usable after a second person reviews it; answers use approved records only.</p>
        </div>
        <div className="row">
          {can("upload") && <Link className="btn" to={`${base}/upload`}>Upload evidence</Link>}
          <Link className="btn" to={`${base}/assistant`}>Ask a question</Link>
        </div>
      </div>

      <div className="tiles">
        {can("drafts") && <Tile label="Awaiting review" value={pending} sub={`${rec("assay", "needs_review")} assays · ${rec("sample", "needs_review")} samples`} to={`${base}/review`} tone={pending ? "warn" : undefined} />}
        <Tile label="Approved records" value={approved} sub={`${rec("assay", "approved")} assays · ${rec("sample", "approved")} samples`} to={`${base}/explorer`} />
        {can("drafts") && <Tile label="Open conflicts" value={s.conflicts.open ?? 0} sub={`${s.conflicts.resolved ?? 0} resolved`} to={`${base}/conflicts`} tone={s.conflicts.open ? "bad" : undefined} />}
        <Tile label="Mapped samples" value={s.mapped_samples} sub={`${s.unmapped_samples} unmapped (not hidden)`} to={`${base}/map`} />
        <Tile label="Source documents" value={s.documents} sub={`${s.stale_records} stale records`} to={`${base}/jobs`} tone={s.stale_records ? "warn" : undefined} />
        <Tile label="Stale answers" value={s.stale_answers} sub="evidence changed since asked" to={`${base}/assistant`} tone={s.stale_answers ? "warn" : undefined} />
      </div>

      <div className="grid cols-2">
        <section className="card">
          <div className="card-head"><h2>Processing</h2><Link to={`${base}/jobs`}>All jobs</Link></div>
          {!jobs.data?.jobs.length ? <Empty>No processing jobs.</Empty> : (
            <table className="data">
              <thead><tr><th>Job</th><th>State</th><th className="num">Pages</th><th>Started</th></tr></thead>
              <tbody>
                {jobs.data.jobs.slice(0, 6).map((j) => (
                  <tr key={j.id}>
                    <td>{j.kind.replace("_", " ")}</td>
                    <td><StatusBadge status={j.state} /></td>
                    <td className="num">{j.cost_counters.pages ?? "—"}</td>
                    <td className="faint">{when(j.created_at)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </section>
        <section className="card">
          <div className="card-head"><h2>Recent answers</h2><Link to={`${base}/assistant`}>Assistant</Link></div>
          {!answers.data?.answers.length ? <Empty>No questions asked yet.</Empty> : (
            <table className="data">
              <thead><tr><th>Question</th><th>State</th><th>Asked</th></tr></thead>
              <tbody>
                {answers.data.answers.slice(0, 6).map((a) => (
                  <tr key={a.id}>
                    <td>{a.question}</td>
                    <td><StatusBadge status={a.state} />{a.stale_reason && <div className="faint">{a.stale_reason}</div>}</td>
                    <td className="faint">{when(a.created_at)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </section>
      </div>
      <p className="faint">
        Your access: {s.roles.join(", ")}{s.can_export ? " · export granted" : ""}. Parser confidence describes extraction reliability only — nothing here estimates resources, reserves or prospectivity.
      </p>
    </div>
  );
}
