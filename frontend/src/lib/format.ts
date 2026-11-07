/** Display helpers. Values are shown exactly as stored strings; no float re-formatting of measurements. */

const COMPARATOR_SYMBOL: Record<string, string> = { EQ: "", LT: "<", LE: "≤", GT: ">", GE: "≥" };

export function qualified(comparator: string | null | undefined, value: string | null | undefined, unit?: string | null): string {
  if (value === null || value === undefined || value === "") return "—";
  return `${COMPARATOR_SYMBOL[comparator ?? "EQ"] ?? ""}${value}${unit ? ` ${unit}` : ""}`;
}

export const OUTCOME_LABEL = {
  YES: "Meets",
  NO: "Does not meet",
  INDETERMINATE: "Indeterminate",
} as const;

const REASONS: Record<string, string> = {
  UNRESOLVED_CONFLICT: "Unresolved conflict between certificates — no governing result selected",
  NOT_GOVERNING_RESULT: "Superseded by the governing result for this sample",
  CONSISTENT_DUPLICATE_RESULT: "Same value reported on another certificate; counted once",
  BASIS_MISMATCH: "Different measurement basis",
  ANALYTE_SPECIES_MISMATCH: "Reported as a different species; not converted",
  RESULT_NOT_NUMERIC: "Result is not numeric",
};
export const reasonText = (code: string) => REASONS[code] ?? code.replaceAll("_", " ").toLowerCase();

export const shortId = (id: string | null | undefined) => (id ? id.slice(0, 8) : "—");

export function bytes(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1024 ** 2) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / 1024 ** 2).toFixed(1)} MB`;
}

export function when(iso: string | null | undefined): string {
  if (!iso) return "—";
  const d = new Date(iso);
  return d.toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
}

export const ROLE_LABEL: Record<string, string> = {
  admin: "Administrator",
  analyst: "Geologist / GIS analyst",
  reviewer: "Data reviewer",
  contributor: "Field contributor",
  viewer: "Read-only",
};

/** Fields shown in the review form, in order, per record kind. */
export const FIELD_ORDER: Record<string, string[]> = {
  assay: ["sample_id", "campaign", "analyte", "result_raw", "unit", "basis", "method", "detection_limit", "detection_limit_unit", "certificate_number", "laboratory"],
  sample: ["sample_id", "campaign", "sample_type", "collection_date", "raw_x", "raw_y", "source_crs", "axis_order", "crs_confirmed", "uncertainty_m", "positioning_method"],
  certificate: ["certificate_number", "laboratory", "issue_date", "revision_label", "methods"],
};

export const FIELD_LABEL: Record<string, string> = {
  sample_id: "Sample ID",
  campaign: "Campaign",
  analyte: "Analyte",
  result_raw: "Result (as reported)",
  unit: "Unit",
  basis: "Basis",
  method: "Method",
  detection_limit: "Detection limit",
  detection_limit_unit: "DL unit",
  certificate_number: "Certificate",
  laboratory: "Laboratory",
  sample_type: "Sample type",
  collection_date: "Collected",
  raw_x: "X / Easting (raw)",
  raw_y: "Y / Northing (raw)",
  source_crs: "Source CRS",
  axis_order: "Axis order",
  crs_confirmed: "CRS confirmed",
  uncertainty_m: "Uncertainty (m)",
  positioning_method: "Positioning",
  issue_date: "Issue date",
  revision_label: "Revision",
  methods: "Methods",
  comparator: "Qualifier",
  value: "Bound / value",
};

export function recordTitle(r: { kind: string; data?: Record<string, unknown> }): string {
  const d = r.data ?? {};
  if (r.kind === "assay") return `${d.sample_id ?? "?"} · ${d.analyte ?? "?"}`;
  if (r.kind === "certificate") return String(d.certificate_number ?? "Certificate");
  return String(d.sample_id ?? "Sample");
}
