/** FR22 deletion confirmation: reason + typed name, then the tombstone summary of what was removed. */
import { useState } from "react";
import { api } from "../lib/api";
import { useAction } from "../lib/hooks";
import { ErrorNotice } from "./ui";

export interface Tombstone {
  id: string;
  target_type: string;
  manifest: { removed: Record<string, number | string[]>; backup_expiry_due: string; audit_events_scrubbed?: number; retained: string };
}

const LABEL: Record<string, string> = {
  objects_deleted: "original files deleted",
  objects_retained_shared: "files kept (still used elsewhere)",
  page_texts: "pages of extracted text",
  anchors: "source anchors",
  records: "records",
  records_other: "other records",
  record_revisions: "record revisions",
  answers_redacted: "answers redacted",
  answers_deleted: "answers deleted",
  exports_revoked: "exports withdrawn",
  exports_deleted: "exports deleted",
  import_previews: "import previews",
  jobs: "processing jobs",
  conflict_groups_changed: "conflict groups updated",
  conflict_groups: "conflict groups",
  config_revisions: "configuration revisions",
  upload_sessions: "upload sessions",
};

export function TombstoneSummary({ t }: { t: Tombstone }) {
  const rows = Object.entries(t.manifest.removed).filter(([k, v]) => k !== "removed_sha256" && Number(v) > 0);
  return (
    <div className="notice ok">
      <div>
        <strong>Deleted. Tombstone {t.id.slice(0, 8)} kept.</strong>
        {rows.map(([k, v]) => `${v} ${LABEL[k] ?? k.replaceAll("_", " ")}`).join(" · ")}
        {t.manifest.audit_events_scrubbed ? ` · ${t.manifest.audit_events_scrubbed} audit entries scrubbed` : ""}.
        <div className="faint">Backup copies expire by {t.manifest.backup_expiry_due}. Kept: {t.manifest.retained}.</div>
      </div>
    </div>
  );
}

export function DeleteForm({ url, what, confirmText, onDone, onCancel }: {
  url: string;
  what: string;
  confirmText: string;
  onDone: (t: Tombstone) => void;
  onCancel?: () => void;
}) {
  const [reason, setReason] = useState("");
  const [typed, setTyped] = useState("");
  const run = useAction(async () => onDone(await api.del<Tombstone>(url, { reason, confirm: typed })));
  return (
    <div className="stack danger-box">
      <div>
        <strong>Delete {what}</strong>
        <div className="muted">
          Removes the original file, its extracted text and anchors, and every record derived from it. Answers that used it lose
          those excerpts, and exports containing it are withdrawn. This cannot be undone; only a tombstone with counts and file
          hashes remains.
        </div>
      </div>
      <label className="field">Reason (recorded on the tombstone)
        <input value={reason} onChange={(e) => setReason(e.target.value)} placeholder="e.g. Client withdrew permission for this report" />
      </label>
      <label className="field">Type <code>{confirmText}</code> to confirm
        <input value={typed} onChange={(e) => setTyped(e.target.value)} autoComplete="off" />
      </label>
      <ErrorNotice error={run.error} />
      <div className="row">
        <button className="danger" disabled={typed !== confirmText || reason.trim().length < 5 || run.pending} onClick={run.run}>
          {run.pending ? "Deleting…" : `Delete ${what}`}
        </button>
        {onCancel && <button className="ghost" onClick={onCancel}>Cancel</button>}
      </div>
    </div>
  );
}
