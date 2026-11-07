import { useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { api } from "../lib/api";
import { useAction, useResource } from "../lib/hooks";
import { Empty, ErrorNotice, Loading } from "../components/ui";

export default function Projects() {
  const nav = useNavigate();
  const list = useResource(() => api.get<{ projects: { id: string; name: string }[] }>("/projects"), []);
  const [name, setName] = useState("");
  const create = useAction(async () => {
    const p = await api.post<{ id: string }>("/projects", { name });
    nav(`/p/${p.id}/admin`);
  });
  return (
    <div className="page">
      <div className="page-head">
        <div>
          <h1>Projects</h1>
          <p>Projects are the access boundary: you only see projects where you hold a current membership.</p>
        </div>
      </div>
      <div className="card">
        {list.loading ? <Loading /> : list.error ? <ErrorNotice error={list.error} /> : list.data!.projects.length === 0 ? (
          <Empty>No projects yet.</Empty>
        ) : (
          <table className="data">
            <thead><tr><th>Name</th><th /></tr></thead>
            <tbody>
              {list.data!.projects.map((p) => (
                <tr key={p.id} className="clickable" onClick={() => nav(`/p/${p.id}`)}>
                  <td><Link to={`/p/${p.id}`}>{p.name}</Link></td>
                  <td className="faint mono">{p.id}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
      <form className="card card-pad row" onSubmit={(e) => { e.preventDefault(); create.run(); }}>
        <label className="field" style={{ flex: 1 }}>
          New project name
          <input value={name} onChange={(e) => setName(e.target.value)} placeholder="e.g. Nasarawa Li prospect" />
        </label>
        <button className="primary" disabled={!name.trim() || create.pending} style={{ alignSelf: "flex-end" }}>Create project</button>
        <ErrorNotice error={create.error} />
      </form>
    </div>
  );
}
