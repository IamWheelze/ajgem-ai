/** Typed client for the AJGEM backend. All calls go through /api (Vite proxy or reverse proxy). */

export const API_BASE = (import.meta.env.VITE_API_BASE as string | undefined) ?? "/api";

const USER_KEY = "ajgem.devUserId";

export function getUserId(): string | null {
  try {
    return localStorage.getItem(USER_KEY);
  } catch {
    return null;
  }
}

export function setUserId(id: string | null): void {
  try {
    if (id) localStorage.setItem(USER_KEY, id);
    else localStorage.removeItem(USER_KEY);
  } catch {
    /* storage unavailable: session-only login */
  }
}

export class ApiError extends Error {
  constructor(
    public status: number,
    public code: string,
    message: string,
    public details?: unknown,
    public correlationId?: string,
  ) {
    super(message);
  }
}

type Init = Omit<RequestInit, "body"> & { body?: unknown; raw?: BodyInit; query?: Record<string, unknown> };

export async function request<T>(path: string, init: Init = {}): Promise<T> {
  const headers = new Headers(init.headers);
  const uid = getUserId();
  if (uid) headers.set("X-User-Id", uid);
  let body: BodyInit | undefined = init.raw;
  if (init.body !== undefined) {
    headers.set("Content-Type", "application/json");
    body = JSON.stringify(init.body);
  }
  let url = API_BASE + path;
  if (init.query) {
    const q = new URLSearchParams();
    for (const [k, v] of Object.entries(init.query)) if (v !== undefined && v !== null && v !== "") q.set(k, String(v));
    const s = q.toString();
    if (s) url += (url.includes("?") ? "&" : "?") + s;
  }
  const res = await fetch(url, { ...init, headers, body });
  if (res.status === 204) return undefined as T;
  const type = res.headers.get("content-type") ?? "";
  if (!res.ok) {
    if (type.includes("json")) {
      const j = await res.json();
      throw new ApiError(res.status, j?.error?.code ?? "ERROR", j?.error?.message ?? res.statusText, j?.error?.details, j?.correlation_id);
    }
    throw new ApiError(res.status, "NETWORK", res.statusText || "Request failed");
  }
  if (type.includes("json")) return (await res.json()) as T;
  return (await res.blob()) as unknown as T;
}

export const api = {
  get: <T>(p: string, query?: Record<string, unknown>) => request<T>(p, { query }),
  post: <T>(p: string, body?: unknown, headers?: HeadersInit) => request<T>(p, { method: "POST", body: body ?? {}, headers }),
  put: <T>(p: string, body?: unknown) => request<T>(p, { method: "PUT", body }),
  del: <T>(p: string) => request<T>(p, { method: "DELETE" }),
};

/* ------------------------------------------------------------------ types */
export type Severity = "critical" | "warning" | "info";
export interface Issue {
  code: string;
  severity: Severity;
  field: string | null;
  message: string;
  kind?: string;
}

export type RecordKind = "sample" | "assay" | "certificate";
export interface RecordView {
  id: string;
  kind: RecordKind;
  status: "needs_review" | "approved" | "rejected";
  stale: boolean;
  current_revision_no: number;
  approved_revision_no: number | null;
  source_version_id: string | null;
  revision_id?: string;
  revision_no?: number;
  revision_state?: string;
  data?: Record<string, string | boolean | null>;
  issues?: Issue[];
  anchor_ids?: string[];
  field_anchors?: Record<string, string[]>;
  extraction_method?: string;
  extraction_uncertainty?: string;
  created_by?: string;
  decided_by?: string | null;
  geometry?: { type: "Point"; coordinates: [number, number] } | null;
  transform?: Record<string, unknown> | null;
}

export interface AnchorView {
  available: boolean;
  reason?: string;
  message?: string;
  version?: { id: string; document_id: string; version_no: number; filename: string; sha256: string; status: string; superseded: boolean };
  anchor?: {
    id: string;
    kind: "pdf_text" | "table_cell";
    page_index: number | null;
    page_label: string | null;
    bbox: number[] | null;
    text_start: number | null;
    text_end: number | null;
    sheet: string | null;
    row_number: number | null;
    column: string | null;
    cell_ref: string | null;
    raw_text: string;
    bbox_convention?: string | null;
    confidence?: number | null;
  };
  page?: {
    method: string;
    size: [number, number] | null;
    image_url: string;
    quality: { method?: string; engine?: string; mean_confidence?: number | null; low_confidence_words?: number; rotation_applied?: number; flags?: string[] };
  } | null;
  context?: string | null;
  context_highlight?: [number, number] | null;
  view_url?: string;
}

export interface Summary {
  project: { id: string; name: string };
  roles: string[];
  can_export: boolean;
  records: Record<string, Record<string, number>>;
  stale_records: number;
  conflicts: Record<string, number>;
  jobs: Record<string, number>;
  documents: number;
  stale_answers: number;
  mapped_samples: number;
  unmapped_samples: number;
}

export interface JobView {
  id: string;
  kind: string;
  state: string;
  attempt: number;
  version_id: string | null;
  created_at?: string;
  manifest: { units?: { unit: string; state: string; attempts: number; candidates: number; issues: Issue[] }[]; summary?: Record<string, number>; partial?: boolean; note?: string };
  cost_counters: Record<string, number>;
  retryable: string[];
}

export interface DocumentList {
  documents: { id: string; title: string; versions: { id: string; version_no: number; filename: string; format: string; status: string; sha256: string; size: number; scan_status: string; created_at: string }[] }[];
}

export type Outcome = "YES" | "NO" | "INDETERMINATE";
export interface ThresholdRow {
  record_id: string;
  revision_id: string;
  revision_no: number;
  sample_id: string;
  certificate_number: string | null;
  analyte: string;
  result_raw: string;
  comparator: string;
  value: string;
  unit: string;
  basis: string;
  method: string | null;
  compared_value: string;
  compared_unit: string;
  derived: string | null;
  outcome: Outcome;
  anchor_ids: string[];
  stale: boolean;
  mapped: boolean;
  equivalent?: { analyte: string; comparator: string; exact_pct: string; display_pct: string; factor: string; factor_version: string; rounding: string; note: string };
}
export interface Exclusion {
  record_id: string;
  sample_id: string;
  certificate_number: string | null;
  reason: string;
  conflict_group_id?: string;
}
export interface ToolResult {
  tool: string;
  tool_version: string;
  query?: { analyte: string; op: string; threshold: string; unit: string; basis: string | null };
  counts?: Record<Outcome, number>;
  rows: (ThresholdRow & Record<string, unknown>)[];
  exclusions?: Exclusion[];
  calculation?: Record<string, unknown>;
  unmapped_matching_samples?: number;
  count?: number;
  note?: string;
  answer_id?: string;
  state?: string;
}
export interface StructuredAnswer {
  kind: "structured";
  answer_id: string | null;
  state: string | null;
  source_statements: { record_id: string; statement: string; anchor_ids: string[] }[];
  calculations: ToolResult[];
  interpretations: unknown[];
  result: ToolResult;
  citations: { anchor_ids: string[]; unresolved: string[]; supported: boolean };
  insufficient_evidence: boolean;
}
export interface SearchResult {
  query: string;
  exact: { record_id: string; kind: string; status: string; sample_id?: string; certificate_number?: string; match: string }[];
  full_text: { version_id: string; page_index: number; page_label: string | null; snippet: string; offset: number }[];
  semantic: { status: string; message: string };
}
export interface RetrievalAnswer {
  kind: "retrieval_only";
  status: string;
  message: string;
  search: SearchResult;
  insufficient_evidence: boolean;
}
export type Answer = StructuredAnswer | RetrievalAnswer;

export interface ConflictView {
  id: string;
  key: string;
  status: "open" | "resolved" | "consistent";
  version: number;
  governing_record_id: string | null;
  reason: string | null;
  members: { record_id: string; status: string; revision_no: number; certificate_number: string | null; result_raw: string | null; unit: string | null; source_version_id: string | null; anchor_ids: string[]; governing: boolean }[];
}

export interface FeatureCollection {
  type: "FeatureCollection";
  unmapped_count: number;
  features: {
    type: "Feature";
    id: string;
    geometry: { type: "Point"; coordinates: [number, number] };
    properties: {
      record_id: string;
      revision_id: string;
      sample_id: string;
      sample_key: string;
      campaign: string | null;
      uncertainty_m: string | null;
      transform: Record<string, unknown> | null;
      assays: { record_id: string; analyte: string; result_raw: string; unit: string; censored: boolean }[];
      any_censored: boolean;
      anchor_ids: string[];
    };
  }[];
}

export interface ProjectDetail {
  id: string;
  name: string;
  roles: string[];
  config: null | { revision: number; boundary: GeoJSON.Polygon | GeoJSON.MultiPolygon | null; working_crs: string | null; created_by: string; created_at: string };
}

/** Page through /records until exhausted (or `max`). */
export async function fetchRecords(pid: string, params: Record<string, unknown> = {}, max = 5000): Promise<RecordView[]> {
  const out: RecordView[] = [];
  let cursor: string | null = null;
  do {
    const page: { records: RecordView[]; next_cursor: string | null } = await api.get(`/projects/${pid}/records`, { ...params, limit: 500, cursor });
    out.push(...page.records);
    cursor = page.next_cursor;
  } while (cursor && out.length < max);
  return out;
}
