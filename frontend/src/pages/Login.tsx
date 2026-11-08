import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { api } from "../lib/api";
import { type AuthConfig, getAuthConfig, startLogin } from "../lib/auth";
import { useResource } from "../lib/hooks";
import { useSession } from "../lib/session";
import { ErrorNotice, Loading } from "../components/ui";

interface DevUser {
  id: string;
  email: string;
  name: string;
  organisation: string;
}

export default function Login() {
  const [cfg, setCfg] = useState<AuthConfig | null>(null);
  const [err, setErr] = useState<unknown>();
  useEffect(() => { getAuthConfig().then(setCfg, setErr); }, []);
  if (err) return <div className="login"><div className="card card-pad"><ErrorNotice error={err} title="Cannot reach the AJGEM service" /></div></div>;
  if (!cfg) return <Loading what="Loading sign-in" />;
  return cfg.mode === "oidc" ? <OrgSignIn cfg={cfg} /> : <DevLogin />;
}

function OrgSignIn({ cfg }: { cfg: Extract<AuthConfig, { mode: "oidc" }> }) {
  const [busy, setBusy] = useState(false);
  const [message] = useState(() => {
    try {
      const m = sessionStorage.getItem("ajgem.authMessage");
      sessionStorage.removeItem("ajgem.authMessage");
      return m;
    } catch {
      return null;
    }
  });
  return (
    <div className="login">
      <div className="card">
        <div className="card-head"><div className="row"><div className="brand-mark" aria-hidden>AJ</div><h1>AJGEM Evidence</h1></div></div>
        <div className="card-pad stack">
          {message && <div className="notice warn"><div><strong>Signed out</strong>{message}</div></div>}
          <p className="muted" style={{ margin: 0 }}>Sign in with your organisation account. Administrators and reviewers need a second factor (MFA) for approvals and administration.</p>
          <button className="primary" disabled={busy} onClick={() => { setBusy(true); startLogin(cfg); }}>{busy ? "Redirecting…" : "Sign in with your organisation"}</button>
          <span className="faint">Identity provider: {new URL(cfg.issuer).host}</span>
        </div>
      </div>
    </div>
  );
}

function DevLogin() {
  const { signIn } = useSession();
  const nav = useNavigate();
  const users = useResource(() => api.get<{ users: DevUser[] }>("/dev/users"), []);
  const byOrg = new Map<string, DevUser[]>();
  for (const u of users.data?.users ?? []) byOrg.set(u.organisation, [...(byOrg.get(u.organisation) ?? []), u]);
  return (
    <div className="login">
      <div className="card">
        <div className="card-head">
          <div className="row">
            <div className="brand-mark" aria-hidden>AJ</div>
            <h1>AJGEM Evidence</h1>
          </div>
        </div>
        <div className="card-pad stack">
          <div className="notice warn">
            <div>
              <strong>Development sign-in</strong>
              Choose a user to act as. This replaces managed identity (OIDC with MFA, FRD NF02) and must not be used with real project data.
            </div>
          </div>
          {users.loading && <Loading what="Loading users" />}
          {users.error && <ErrorNotice error={users.error} title="Development sign-in is disabled or the API is unreachable" />}
          {users.data?.users.length === 0 && (
            <div className="faint">
              No users yet. Run <code>python scripts/seed_demo.py</code> or <code>python -m ajgem create-user ORG EMAIL</code> in <code>backend/</code>.
            </div>
          )}
          {[...byOrg].map(([org, us]) => (
            <div key={org} className="stack">
              <h3>{org}</h3>
              {us.map((u) => (
                <button key={u.id} className="user-btn" onClick={() => { signIn(u.id); nav("/projects"); }}>
                  <span>{u.name}</span>
                  <span className="faint">{u.email}</span>
                </button>
              ))}
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}
