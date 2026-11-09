/** FR13 source viewer: opens the exact immutable version and locator behind a field or citation. */
import { useEffect, useRef, useState } from "react";
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
          <div className="row" style={{ gap: 6 }}>
            <span className="faint">Page {a.page_label ?? (a.page_index ?? 0) + 1} · characters {a.text_start}–{a.text_end}</span>
            {v.page?.quality.method === "ocr" && <span className="badge info" title={v.page.quality.engine}>OCR</span>}
            {typeof a.confidence === "number" && (
              <span className={`badge ${a.confidence < 60 ? "bad" : a.confidence < 80 ? "warn" : "ok"}`} title="Lowest OCR word confidence in this region">
                {Math.round(a.confidence)}% confidence
              </span>
            )}
            {!!v.page?.quality.rotation_applied && <span className="badge">rotated {v.page.quality.rotation_applied}°</span>}
            {v.page?.quality.flags?.includes("LOW_PAGE_CONFIDENCE") && <span className="badge warn">poor scan</span>}
            {!a.bbox && v.page?.quality.method !== "ocr" && <span className="faint">· text layer; this text could not be placed on the page image</span>}
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
        {a.kind === "pdf_text" && v.page && (
          <button className="small" onClick={() => setOpen((o) => !o)} aria-expanded={open}>
            {open ? "Hide page" : a.bbox ? "Show region on page" : "Show page"}
          </button>
        )}
        <span className="faint">anchor {shortId(a.id)}</span>
      </div>
      {open && a.kind === "pdf_text" && v.page && (
        <PageImage url={`${API_BASE}${v.page.image_url}`} size={v.page.size} bbox={a.bbox} alt={`${v.version!.filename} page ${(a.page_index ?? 0) + 1}`} />
      )}
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

/** Rendered page image with the anchored region outlined (bbox in PDF points, origin top-left). */
export function PageImage({ url, size, bbox, alt }: { url: string; size: [number, number] | null; bbox: number[] | null; alt: string }) {
  const box = useRef<HTMLDivElement>(null);
  const mark = useRef<HTMLDivElement>(null);
  const [failed, setFailed] = useState(false);
  const pct = (v: number, of: number) => `${(v / of) * 100}%`;
  const onLoad = () => {
    if (box.current && mark.current) box.current.scrollTop = mark.current.offsetTop - box.current.clientHeight / 2;
  };
  if (failed) return <div className="notice warn"><div>Page image unavailable; use “Open original”.</div></div>;
  return (
    <div className="page-image" ref={box}>
      <div style={{ position: "relative" }}>
        <img src={url} alt={alt} onLoad={onLoad} onError={() => setFailed(true)} />
        {bbox && size && (
          <div ref={mark} className="page-mark" aria-label="Anchored region"
            style={{ left: pct(bbox[0] - 3, size[0]), top: pct(bbox[1] - 3, size[1]), width: pct(bbox[2] - bbox[0] + 6, size[0]), height: pct(bbox[3] - bbox[1] + 6, size[1]) }} />
        )}
      </div>
    </div>
  );
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
