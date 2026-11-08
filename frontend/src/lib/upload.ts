/** Resumable, verified chunked upload (FR03). Survives reloads: the server keeps confirmed ranges. */
import { API_BASE, ApiError, api, authHeaders } from "./api";

export const CHUNK_BYTES = 1024 * 1024;

export interface UploadSession {
  id: string;
  status: string;
  total_size: number;
  received: [number, number][];
  missing: [number, number][];
  expires_at: string;
  result: UploadResult | null;
}

export type UploadResult =
  | { status: "stored"; document_id: string; version_id: string; version_no: number; format: string; supersedes: string | null; impact: { record_id: string; kind: string; status: string }[]; job_id?: string }
  | { status: "duplicate"; existing_version_id: string; existing_document_id: string; message: string }
  | { status: "quarantined"; reason: string };

/** Split missing byte ranges into chunk-sized [start, end) pieces. */
export function planChunks(missing: [number, number][], size = CHUNK_BYTES): [number, number][] {
  const out: [number, number][] = [];
  for (const [a, b] of missing) for (let s = a; s < b; s += size) out.push([s, Math.min(b, s + size)]);
  return out;
}

export function receivedBytes(received: [number, number][]): number {
  return received.reduce((n, [a, b]) => n + (b - a), 0);
}

export async function sha256Hex(data: ArrayBuffer): Promise<string> {
  const d = await crypto.subtle.digest("SHA-256", data);
  return Array.from(new Uint8Array(d), (b) => b.toString(16).padStart(2, "0")).join("");
}

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

export interface UploadOptions {
  projectId: string;
  file: File;
  rightsStatement: string;
  onProgress?: (sent: number, total: number, phase: "hashing" | "uploading" | "verifying") => void;
  signal?: AbortSignal;
}

/** Start or resume an upload session and send every missing chunk. Returns the session ready to complete. */
export async function sendFile(o: UploadOptions): Promise<UploadSession> {
  o.onProgress?.(0, o.file.size, "hashing");
  const buf = await o.file.arrayBuffer();
  const sha = await sha256Hex(buf);
  // The Idempotency-Key is derived from content, so retrying after a reload resumes the same session.
  const session = await api.post<UploadSession>(
    `/projects/${o.projectId}/uploads`,
    { filename: o.file.name, size: o.file.size, sha256: sha, media_type: o.file.type || "application/octet-stream", rights_confirmed: true, rights_statement: o.rightsStatement },
    { "Idempotency-Key": `${sha}:${o.file.name}` },
  );
  let sent = receivedBytes(session.received);
  o.onProgress?.(sent, o.file.size, "uploading");
  for (const [a, b] of planChunks(session.missing)) {
    const part = buf.slice(a, b);
    const partSha = await sha256Hex(part);
    for (let attempt = 0; ; attempt++) {
      if (o.signal?.aborted) throw new DOMException("Upload paused", "AbortError");
      try {
        const res = await fetch(`${API_BASE}/uploads/${session.id}/chunks?offset=${a}`, {
          method: "PUT",
          body: part,
          signal: o.signal,
          headers: { ...authHeaders(), "X-Chunk-SHA256": partSha, "Content-Type": "application/octet-stream" },
        });
        if (!res.ok) {
          const j = await res.json().catch(() => null);
          throw new ApiError(res.status, j?.error?.code ?? "NETWORK", j?.error?.message ?? res.statusText);
        }
        break;
      } catch (e) {
        if ((e as Error).name === "AbortError") throw e;
        const retryable = !(e instanceof ApiError) || e.status >= 500 || e.code === "CHECKSUM_MISMATCH";
        if (!retryable || attempt >= 3) throw e;
        await sleep(500 * 2 ** attempt);
      }
    }
    sent += b - a;
    o.onProgress?.(sent, o.file.size, "uploading");
  }
  o.onProgress?.(o.file.size, o.file.size, "verifying");
  return api.get<UploadSession>(`/uploads/${session.id}`);
}

export function completeUpload(sessionId: string, opts: { replaces_document_id?: string; as_new_document?: boolean } = {}) {
  return api.post<UploadResult>(`/uploads/${sessionId}/complete`, opts);
}
