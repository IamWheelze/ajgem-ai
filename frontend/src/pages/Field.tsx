/** FR25 offline field capture: drafts saved on this device, synced through conflict review. */
import { useCallback, useEffect, useState } from "react";
import { api, ApiError } from "../lib/api";
import { editDraft, newDraft, store, type DeviceCreds, type LocalDraft } from "../lib/offline";
import { useProject } from "../components/Layout";
import { Empty, ErrorNotice, StatusBadge } from "../components/ui";

const FIELDS = ["sample_id", "campaign", "sample_type", "collection_date", "raw_x", "raw_y", "source_crs",
                "uncertainty_m", "positioning_method"] as const;

interface ServerDraft { id: string; sample_key: string; status: string; created_by: string; data: Record<string, string>;
                        client_revision: number; synced_at: string; resolution_reason: string | null }

export default function Field() {
  const { pid, roles } = useProject();
  const canCapture = roles.has("analyst") || roles.has("contributor") || roles.has("reviewer");
  const canResolve = roles.has("reviewer");
  const [device, setDevice] = useState<DeviceCreds | null>(null);
  const [drafts, setDrafts] = useState<LocalDraft[]>([]);
  const [form, setForm] = useState<Record<string, string>>({});
  const [editing, setEditing] = useState<LocalDraft | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [server, setServer] = useState<ServerDraft[]>([]);
  const [reason, setReason] = useState("");

  const reload = useCallback(async () => {
    if (await store.enforceExpiry()) setNotice("The local field cache expired and was deleted from this device.");
    setDevice((await store.device()) ?? null);
    setDrafts(await store.drafts(pid));
    try {
      setServer((await api.get<{ drafts: ServerDraft[] }>(`/projects/${pid}/field/drafts`)).drafts);
    } catch { /* offline: local drafts still shown */ }
  }, [pid]);
  useEffect(() => { void reload(); }, [reload]);

  async function register() {
    setError(null);
    try {
      const r = await api.post<{ id: string; device_secret: string; name: string; cache_expires_at: string }>(
        "/devices", { name: navigator.userAgent.slice(0, 60) || "Field device", cache_ttl_hours: 72 });
      await store.setDevice({ id: r.id, secret: r.device_secret, name: r.name, cache_expires_at: r.cache_expires_at });
      await reload();
    } catch (e) { setError(e); }
  }

  async function save() {
    const data = Object.fromEntries(Object.entries(form).filter(([, v]) => v.trim() !== ""));
    if (!data.sample_id) { setError(new Error("Sample ID is required")); return; }
    await store.put(editing ? editDraft(editing, data) : newDraft(pid, data));
    setForm({}); setEditing(null); setError(null);
    await reload();
  }

  async function sync() {
    if (!device) return;
    setError(null);
    const dirty = drafts.filter((d) => d.dirty);
    try {
      const r = await api.post<{ results: { id: string; status: LocalDraft["status"] }[]; cache_expires_at: string }>(
        `/projects/${pid}/field/sync`,
        { drafts: dirty.map(({ id, data, client_revision, client_updated_at }) => ({ id, data, client_revision, client_updated_at })) },
        { "X-Device-Id": device.id, "X-Device-Secret": device.secret });
      const byId = new Map(r.results.map((x) => [x.id, x.status]));
      for (const d of dirty) await store.put({ ...d, dirty: false, status: byId.get(d.id) ?? d.status });
      await store.setDevice({ ...device, cache_expires_at: r.cache_expires_at });
      const conflicts = r.results.filter((x) => x.status === "conflict").length;
      setNotice(`Synced ${dirty.length} draft(s)${conflicts ? `; ${conflicts} in conflict, kept for review` : ""}.`);
      await reload();
    } catch (e) {
      if (e instanceof ApiError && e.code === "DEVICE_REVOKED") {
        await store.wipe();
        setNotice("This device was revoked. Its local drafts have been deleted; register again from a trusted device.");
        await reload();
        return;
      }
      setError(e);
    }
  }

  async function resolve(id: string) {
    setError(null);
    try {
      await api.post(`/projects/${pid}/field/resolve`, { keep_draft_id: id, reason });
      setReason("");
      await reload();
    } catch (e) { setError(e); }
  }

  const conflicts = server.filter((d) => d.status === "conflict" || d.status === "pending");
  const groups = new Map<string, ServerDraft[]>();
  for (const d of conflicts) groups.set(d.sample_key, [...(groups.get(d.sample_key) ?? []), d]);

  return (
    <div className="page">
      <h1>Field capture</h1>
      <p className="faint">Drafts are kept on this device and sent when you sync. Drafts by different people for the
        same sample are all kept until a reviewer chooses one; nothing becomes a record before review.</p>
      {notice && <div className="notice" role="status">{notice}</div>}
      {error != null && <ErrorNotice error={error} />}
      {canCapture && (
        <section className="card">
          <h2>This device</h2>
          {device ? (
            <p>Registered as <strong>{device.name}</strong>. Local cache expires{" "}
              <time dateTime={device.cache_expires_at}>{new Date(device.cache_expires_at).toLocaleString()}</time>
              {" "}unless renewed by a sync.</p>
          ) : (
            <button className="btn" onClick={register}>Register this device for offline capture</button>
          )}
        </section>
      )}
      {canCapture && device && (
        <section className="card">
          <h2>{editing ? `Edit draft ${editing.data.sample_id}` : "New observation"}</h2>
          <div className="grid-2">
            {FIELDS.map((f) => (
              <label key={f}>{f.replace(/_/g, " ")}
                <input value={form[f] ?? ""} onChange={(e) => setForm({ ...form, [f]: e.target.value })} />
              </label>
            ))}
          </div>
          <button className="btn" onClick={save}>Save draft on this device</button>
          {editing && <button className="btn ghost" onClick={() => { setEditing(null); setForm({}); }}>Cancel</button>}
        </section>
      )}
      {canCapture && device && (
        <section className="card">
          <h2>Drafts on this device</h2>
          {drafts.length === 0 ? <Empty>No drafts yet.</Empty> : (
            <table>
              <thead><tr><th>Sample</th><th>Revision</th><th>Status</th><th /></tr></thead>
              <tbody>
                {drafts.map((d) => (
                  <tr key={d.id}>
                    <td>{d.data.sample_id}</td><td>{d.client_revision}</td>
                    <td><StatusBadge status={d.dirty ? "unsynced" : d.status} /></td>
                    <td>{(d.status === "unsynced" || d.status === "pending" || d.status === "conflict") &&
                      <button className="btn ghost" onClick={() => { setEditing(d); setForm(d.data); }}>Edit</button>}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
          <button className="btn" onClick={sync} disabled={!drafts.some((d) => d.dirty)}>Sync now</button>
        </section>
      )}
      <section className="card">
        <h2>Synced drafts awaiting a decision</h2>
        {groups.size === 0 ? <Empty>No field drafts are waiting.</Empty> : [...groups].map(([key, ds]) => (
          <div key={key} className="card">
            <h3>{key} {ds.length > 1 && <StatusBadge status="conflict" label={`${ds.length} competing drafts`} />}</h3>
            <table>
              <thead><tr><th>By</th><th>Data</th><th>Synced</th><th /></tr></thead>
              <tbody>
                {ds.map((d) => (
                  <tr key={d.id}>
                    <td>{d.created_by.slice(0, 8)}</td>
                    <td>{Object.entries(d.data).map(([k, v]) => `${k}: ${v}`).join("; ")}</td>
                    <td>{new Date(d.synced_at).toLocaleString()}</td>
                    <td>{canResolve && <button className="btn" disabled={!reason.trim()} onClick={() => resolve(d.id)}>Keep this one</button>}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ))}
        {canResolve && groups.size > 0 && (
          <label>Reason for the decision (required)
            <input value={reason} onChange={(e) => setReason(e.target.value)} />
          </label>
        )}
      </section>
    </div>
  );
}
