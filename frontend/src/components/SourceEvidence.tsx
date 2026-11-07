/** FR13 source viewer: opens the exact immutable version and locator behind a field or citation. */
import { useEffect, useState } from "react";
import { API_BASE, api, type AnchorView } from "../lib/api";
import { useResource } from "../lib/hooks";
import { shortId } from "../lib/format";
import { ErrorNotice, Loading } from "./ui";

export function Highlighted({ text, range }: { text: string; range?: [number, number] | null }) {
  if (!range) return <>{text}</>;
  const [a, b] = range;
  return (
    <>
      {text.slice(0, a)}
      <mark>{text.slice(a, b)}</mark>
      {text.slice(b)}
    </>
  );
}

export function AnchorCard({ anchorId, field, showOriginal = false }: { anchorId: string; field?: string; showOriginal?: boolean }) {
  const r = useResource(() => api.get<AnchorView>(`/anchors/${anchorId}`), [anchorId]);
  const [open, setOpen] = useState(showOriginal);
  useEffect(() => setOpen(showOriginal), [showOriginal, anchorId]);
  if (r.loading) return <Loading what="Opening evidence" />;
  if (r.error) return <ErrorNotice error={r.error} title="Evidence unavailable" />;
  const v = r.data!;
  if (!v.available)
    return (
      <div className="notice bad">
        <div>
          <strong>Evidence unavailable</strong>
          {v.message} This citation cannot support a claim.
        </div>
      </div>
    );
  const a = v.anchor!;
  const src = `${API_BASE}${v.view_url}${a.kind === "pdf_text" && a.page_index !== null ? `#page=${a.page_index + 1}` : ""}`;
  return (
    <div className="card card-pad stack">
      <div className="row between">
        <div className="row">
          {field && <span className="badge accent">{field}</span>}
          <strong>{v.version!.filename}</strong>
          <span className="faint">v{v.version!.version_no}</span>
          {v.version!.superseded && <span className="badge warn">superseded version</span>}
        </div>
        <span className="faint mono" title={v.version!.sha256}>sha256 {v.version!.sha256.slice(0, 12)}…</span>
      </div>
      {a.kind === "pdf_text" ? (
        <>
          <div className="faint">
            Page {a.page_label ?? (a.page_index ?? 0) + 1} · characters {a.text_start}–{a.text_end}
            {!a.bbox && " · word boxes not available for this extraction"}
          </div>
          <div className="evidence-text">{v.context ? <Highlighted text={v.context} range={v.context_highlight} /> : a.raw_text}</div>
        </>
      ) : (
        <dl className="cell-locator">
          {a.sheet && (<><dt>Sheet</dt><dd>{a.sheet}</dd></>)}
          <dt>Row</dt><dd>{a.row_number}</dd>
          <dt>Column</dt><dd>{a.column}</dd>
          <dt>Cell</dt><dd>{a.cell_ref}</dd>
          <dt>Raw value</dt><dd><mark>{a.raw_text || "(empty)"}</mark></dd>
        </dl>
      )}
      <div className="row">
        <a className="btn small" href={src} target="_blank" rel="noreferrer">Open original ↗</a>
        {a.kind === "pdf_text" && (
          <button className="small" onClick={() => setOpen((o) => !o)} aria-expanded={open}>
            {open ? "Hide page" : "Show page"}
          </button>
        )}
        <span className="faint">anchor {shortId(a.id)}</span>
      </div>
      {open && a.kind === "pdf_text" && <iframe className="source-frame" title={`${v.version!.filename} page ${(a.page_index ?? 0) + 1}`} src={src} />}
    </div>
  );
}

export interface EvidenceItem {
  anchorId: string;
  field?: string;
}

/** Collapse a record's field→anchor map into unique anchors labelled with the fields they support. */
export function evidenceItems(fieldAnchors: Record<string, string[]> | undefined, anchorIds: string[] = []): EvidenceItem[] {
  const by = new Map<string, string[]>();
  for (const [f, ids] of Object.entries(fieldAnchors ?? {})) for (const id of ids) by.set(id, [...(by.get(id) ?? []), f]);
  for (const id of anchorIds) if (!by.has(id)) by.set(id, []);
  return [...by].map(([anchorId, fs]) => ({ anchorId, field: fs.length ? fs.join(", ") : undefined }));
}

export function SourceDrawer({ items, onClose, title = "Source evidence" }: { items: EvidenceItem[]; onClose: () => void; title?: string }) {
  useEffect(() => {
    const k = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", k);
    return () => window.removeEventListener("keydown", k);
  }, [onClose]);
  return (
    <>
      <div className="drawer-backdrop" onClick={onClose} />
      <aside className="drawer" role="dialog" aria-label={title}>
        <div className="card-head">
          <h2>{title}</h2>
          <button className="ghost" onClick={onClose} aria-label="Close">✕</button>
        </div>
        <div className="drawer-body">
          {items.length === 0 && <div className="notice bad"><div><strong>No anchors</strong>This item has no linked source evidence and cannot be presented as supported.</div></div>}
          {items.map((it) => <AnchorCard key={it.anchorId} {...it} />)}
        </div>
      </aside>
    </>
  );
}
