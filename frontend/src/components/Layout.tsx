import { createContext, useContext, useEffect, type ReactNode } from "react";
import { NavLink, Outlet, useNavigate, useParams } from "react-router-dom";
import { api, type Summary } from "../lib/api";
import { useResource, type Resource } from "../lib/hooks";
import { useSession } from "../lib/session";
import { ErrorNotice, Loading } from "./ui";

interface ProjectCtx {
  pid: string;
  summary: Summary;
  reload: () => void;
  roles: Set<string>;
  can: (op: "review" | "edit" | "upload" | "drafts" | "admin" | "export") => boolean;
}
const Ctx = createContext<ProjectCtx | null>(null);

export function useProject(): ProjectCtx {
  const v = useContext(Ctx);
  if (!v) throw new Error("Project context missing");
  return v;
}

const ROLE_OPS: Record<string, string[]> = {
  review: ["reviewer"],
  edit: ["analyst", "reviewer"],
  upload: ["analyst", "contributor"],
  drafts: ["admin", "analyst", "reviewer", "contributor"],
  admin: ["admin"],
};

export function ProjectLayout() {
  const { pid = "" } = useParams();
  const summary = useResource(() => api.get<Summary>(`/projects/${pid}/summary`), [pid]);
  useEffect(() => {
    const t = setInterval(() => document.visibilityState === "visible" && summary.reload(), 30000);
    return () => clearInterval(t);
  }, [summary]);
  return (
    <Shell pid={pid} summary={summary}>
      {summary.error ? (
        <div className="page"><ErrorNotice error={summary.error} title="Project unavailable" /></div>
      ) : !summary.data ? (
        <Loading />
      ) : (
        <ProjectProvider pid={pid} summary={summary}>
          <Outlet />
        </ProjectProvider>
      )}
    </Shell>
  );
}

function ProjectProvider({ pid, summary, children }: { pid: string; summary: Resource<Summary>; children: ReactNode }) {
  const roles = new Set(summary.data!.roles);
  const value: ProjectCtx = {
    pid,
    summary: summary.data!,
    reload: summary.reload,
    roles,
    can: (op) => (op === "export" ? summary.data!.can_export : ROLE_OPS[op].some((r) => roles.has(r))),
  };
  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

function Shell({ pid, summary, children }: { pid?: string; summary?: Resource<Summary>; children: ReactNode }) {
  const { me, signOut } = useSession();
  const nav = useNavigate();
  const projects = useResource(() => api.get<{ projects: { id: string; name: string }[] }>("/projects"), [me?.id]);
  const s = summary?.data;
  const roles = new Set(s?.roles ?? []);
  const drafts = ["admin", "analyst", "reviewer", "contributor"].some((r) => roles.has(r));
  const pending = Object.values(s?.records ?? {}).reduce((n, k) => n + (k.needs_review ?? 0), 0);
  const conflicts = s?.conflicts?.open ?? 0;
  const link = (to: string, label: string, count?: number, show = true) =>
    show && (
      <NavLink to={`/p/${pid}/${to}`} end={to === ""}>
        <span>{label}</span>
        {!!count && <span className="count">{count}</span>}
      </NavLink>
    );
  return (
    <div className="shell">
      <aside className="side">
        <div className="brand">
          <div className="brand-mark" aria-hidden>AJ</div>
          <div>
            <strong>AJGEM Evidence</strong>
            <small>Exploration records, reviewed</small>
          </div>
        </div>
        <div className="project-switch">
          <select aria-label="Project" value={pid ?? ""} onChange={(e) => nav(e.target.value ? `/p/${e.target.value}` : "/projects")}>
            <option value="">All projects…</option>
            {projects.data?.projects.map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}
          </select>
        </div>
        {pid && (
          <nav className="nav" aria-label="Project">
            {link("", "Dashboard")}
            {link("upload", "Upload & import", undefined, roles.has("analyst") || roles.has("contributor"))}
            {link("review", "Review", pending, drafts)}
            {link("explorer", "Data explorer")}
            {link("map", "Map")}
            {link("assistant", "Assistant", s?.stale_answers)}
            {link("conflicts", "Conflicts & versions", conflicts, drafts)}
            {link("jobs", "Jobs & exports")}
            {link("admin", "Administration", undefined, roles.has("admin") || roles.has("reviewer"))}
          </nav>
        )}
        {!pid && <div className="spacer" />}
        <div className="side-foot">
          <div>
            <strong>{me?.name}</strong> · {me?.email}
            <div className="faint">{me?.organisation.name}</div>
            {s && <div className="faint">Roles: {s.roles.join(", ")}</div>}
          </div>
          <button className="small" onClick={() => { signOut(); nav("/login"); }}>Switch user</button>
          <div className="faint">Development sign-in. Not for real data.</div>
        </div>
      </aside>
      <main className="main">{children}</main>
    </div>
  );
}

export function PlainShell({ children }: { children: ReactNode }) {
  return <Shell>{children}</Shell>;
}
