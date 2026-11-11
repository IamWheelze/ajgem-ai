/** Typed client for the AJGEM backend. All calls go through /api (Vite proxy or reverse proxy). */

import { clearIdToken, getIdToken } from "./auth";

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

/** Credentials for API calls: the OIDC ID token when signed in, else the development user id. */
export function authHeaders(): Record<string, string> {
  const token = getIdToken();
  if (token) return { Authorization: `Bearer ${token}` };
  const uid = getUserId();
  return uid ? { "X-User-Id": uid } : {};
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
  for (const [k, v] of Object.entries(authHeaders())) headers.set(k, v);
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
    const j = type.includes("json") ? await res.json() : null;
    if (res.status === 401 && getIdToken()) {
      // Expired, revoked or unregistered: drop the session and tell the sign-in page why.
      try { sessionStorage.setItem("ajgem.authMessage", j?.error?.message ?? "Please sign in again"); } catch { /* ignore */ }
      clearIdToken();
      window.dispatchEvent(new Event("ajgem:signed-out"));
    }
    if (j) {
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
  del: <T>(p: string, body?: unknown) => request<T>(p, { method: "DELETE", body }),
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
    kind: "pdf_text" | "doc_text" | "table_cell";
    locator?: { renderer: string; paragraph: number; start: number; end: number; drift?: { renderer: string; reason: string } } | null;
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
    bbox_source?: "ocr" | "text_layer" | null;
    confidence?: number | null;
  };
  page?: {
    method: string;
    size: [number, number] | null;
    image_url: string | null;
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
  manifest: {
    units?: { unit: string; state: string; attempts: number; candidates: number; issues: Issue[] }[]; summary?: Record<string, number>; partial?: boolean; note?: string;
    estimate?: { pages: number; max_ocr_pages: number; model_assisted: boolean };
    budget?: string[];
    budget_override?: { by: string; reason: string; at: string };
  };
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
  rows_total?: number;
  rows_truncated?: boolean;
  exclusions?: Exclusion[];
  exclusions_total?: number;
  calculation?: Record<string, unknown>;
  unmapped_matching_samples?: number;
  deletion_notice?: { message: string; deletion_request_id: string };
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
  citations: { anchor_ids: string[]; unresolved: string[]; supported: boolean; count?: number };
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
export interface NarrativeStatement {
  text: string;
  citations: { anchor_id: string; title: string; text: string; version_id: string; page_index: number }[];
}
export interface NarrativeAnswer {
  kind: "narrative";
  answer_id: string;
  state: string;
  status: "answered" | "insufficient_evidence" | "declined";
  statements: NarrativeStatement[];
  suppressed: Partial<Record<"uncited" | "unsupported_figure" | "unresolved_citation", number>>;
  truncated?: boolean;
  retrieval: { method: string; synonyms: string; terms: string[]; passages: { title: string; source: string; blocks: number }[] };
  provider: null | { name: string; model: string; adapter: string; request_id: string | null; usage: Record<string, number>; served_by_fallback: boolean; stop_reason: string | null };
  notice: string;
  message: string | null;
  deletion_notice?: { message: string; deletion_request_id: string };
  citations: { anchor_ids: string[]; unresolved: string[]; supported: boolean; count?: number };
  search?: SearchResult;
}
export type Answer = StructuredAnswer | RetrievalAnswer | NarrativeAnswer;

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
  llm?: { configured: boolean; provider: string | null; model: string | null };
  config: null | {
    revision: number; boundary: GeoJSON.Polygon | GeoJSON.MultiPolygon | null; working_crs: string | null; created_by: string; created_at: string;
    provider_policy?: { allow_external_llm?: boolean; provider?: string | null; notes?: string };
    cost_policy?: CostLimits;
  };
}

/** Page through /records until exhausted (or `max`). */
export interface DateExclusions { undated: number; uncertain: number }

/** All records matching the filters, plus (with date_from/date_to) how many the date filter could not place. */
export async function fetchRecordsDetailed(pid: string, params: Record<string, unknown> = {}, max = 5000): Promise<{ records: RecordView[]; dateExcluded: DateExclusions | null }> {
  const out: RecordView[] = [];
  let excluded = null as DateExclusions | null;
  let cursor: string | null = null;
  do {
    const page: { records: RecordView[]; next_cursor: string | null; date_filter?: { excluded: DateExclusions } } =
      await api.get(`/projects/${pid}/records`, { ...params, limit: 500, cursor });
    out.push(...page.records);
    if (page.date_filter) {
      const e = page.date_filter.excluded;
      excluded = { undated: (excluded?.undated ?? 0) + e.undated, uncertain: (excluded?.uncertain ?? 0) + e.uncertain };
    }
    cursor = page.next_cursor;
  } while (cursor && out.length < max);
  return { records: out, dateExcluded: excluded };
}

export async function fetchRecords(pid: string, params: Record<string, unknown> = {}, max = 5000): Promise<RecordView[]> {
  return (await fetchRecordsDetailed(pid, params, max)).records;
}

export interface CostLimits { monthly_pages?: number | null; monthly_model_tokens?: number | null }
export interface UsageRow { events: number; quantity: number; input_tokens: number; output_tokens: number }
export interface UsageStatus {
  period_start: string;
  usage: Record<"pages" | "ocr_pages" | "model_extraction" | "model_answers" | "export_bytes", UsageRow>;
  limits: { project: CostLimits; organisation: CostLimits };
  used: { project: { monthly_pages: number; monthly_model_tokens: number }; organisation: { monthly_pages: number; monthly_model_tokens: number } };
  remaining: { project: CostLimits; organisation: CostLimits };
  storage_bytes: number;
}
