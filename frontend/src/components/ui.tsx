import type { ReactNode } from "react";
import { Link } from "react-router-dom";
import { ApiError, type Issue, type Outcome } from "../lib/api";
import { OUTCOME_LABEL } from "../lib/format";
import { useSession } from "../lib/session";

export function ErrorNotice({ error, title }: { error: unknown; title?: string }) {
  if (!error) return null;
  const e = error as ApiError;
  const details = e instanceof ApiError ? e.details : undefined;
  return (
    <div className="notice bad" role="alert">
      <div>
        <strong>{title ?? (e instanceof ApiError ? e.code.replaceAll("_", " ") : "Something went wrong")}</strong>
        <div>{e.message}</div>
        {details !== undefined && (
          <details>
            <summary>Details</summary>
            <pre className="json">{JSON.stringify(details, null, 2)}</pre>
          </details>
        )}
        {e instanceof ApiError && e.code === "MFA_REQUIRED" && <MfaRetry />}
        {e instanceof ApiError && e.correlationId && <div className="faint">Correlation ID {e.correlationId}</div>}
      </div>
    </div>
  );
}

function MfaRetry() {
  const { signOut } = useSession();
  return (
    <div style={{ marginTop: 6 }}>
      <button className="small" onClick={() => { signOut(); window.location.assign("/login"); }}>Sign in again with your second factor</button>
    </div>
  );
}

export function Loading({ what = "Loading" }: { what?: string }) {
  return <div className="empty" aria-busy="true">{what}…</div>;
}

export function Empty({ children }: { children: ReactNode }) {
  return <div className="empty">{children}</div>;
}

const STATUS: Record<string, string> = {
  approved: "ok",
  needs_review: "warn",
  rejected: "bad",
  superseded: "",
  open: "bad",
  resolved: "ok",
  consistent: "info",
  current: "ok",
  stale: "warn",
  evidence_removed: "bad",
  completed: "ok",
  needs_review_job: "warn",
  partial_failed: "bad",
  failed: "bad",
  processing: "info",
  queued: "info",
  cancelled: "",
  succeeded: "ok",
  flagged: "warn",
  active: "ok",
};

export function StatusBadge({ status, label }: { status: string; label?: string }) {
  return <span className={`badge ${STATUS[status] ?? ""}`}>{label ?? status.replaceAll("_", " ")}</span>;
}

export function OutcomeBadge({ outcome }: { outcome: Outcome }) {
  const cls = outcome === "YES" ? "ok" : outcome === "NO" ? "" : "indet";
  return <span className={`badge ${cls}`} title={outcome === "INDETERMINATE" ? "A censored value cannot be decided against this threshold" : undefined}>{OUTCOME_LABEL[outcome]}</span>;
}

export function IssueList({ issues, ack, onAck }: { issues: Issue[]; ack?: Set<string>; onAck?: (code: string, on: boolean) => void }) {
  if (!issues.length) return <div className="faint">No validation issues.</div>;
  const order = { critical: 0, warning: 1, info: 2 } as const;
  return (
    <div className="stack" role="list">
      {[...issues].sort((a, b) => order[a.severity] - order[b.severity]).map((i, n) => (
        <div key={n} className={`issue ${i.severity}`} role="listitem">
          <span className={`badge ${i.severity === "critical" ? "bad" : i.severity === "warning" ? "warn" : "info"}`}>{i.severity}</span>
          <div style={{ flex: 1 }}>
            <div>
              <code>{i.code}</code>
              {i.field && <span className="faint"> · {i.field}</span>}
            </div>
            <div className="muted">{i.message}</div>
          </div>
          {i.severity === "warning" && onAck && (
            <label className="check faint">
              <input type="checkbox" checked={ack?.has(i.code) ?? false} onChange={(e) => onAck(i.code, e.target.checked)} />
              Acknowledge
            </label>
          )}
        </div>
      ))}
    </div>
  );
}

export function Tile({ label, value, sub, to, tone }: { label: string; value: ReactNode; sub?: ReactNode; to?: string; tone?: "warn" | "bad" | "ok" }) {
  const color = tone ? { warn: "var(--warn)", bad: "var(--bad)", ok: "var(--ok)" }[tone] : undefined;
  const inner = (
    <>
      <span className="label">{label}</span>
      <span className="value" style={{ color }}>{value}</span>
      {sub && <span className="sub">{sub}</span>}
    </>
  );
  return to ? <Link className="card tile" to={to}>{inner}</Link> : <div className="card tile">{inner}</div>;
}
