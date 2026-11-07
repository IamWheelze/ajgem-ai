import { planChunks, receivedBytes } from "./upload";
import { qualified, reasonText } from "./format";
import { circlePolygon, inBBox, normaliseBBox } from "./geo";
import { guessColumns } from "../components/ImportMapper";
import { evidenceItems } from "../components/SourceEvidence";

describe("upload planning", () => {
  it("resumes only the missing ranges, chunked", () => {
    expect(planChunks([[40, 100]], 25)).toEqual([[40, 65], [65, 90], [90, 100]]);
    expect(planChunks([], 25)).toEqual([]);
    expect(receivedBytes([[0, 40], [60, 70]])).toBe(50);
  });
});

describe("qualified values", () => {
  it("shows qualifiers exactly and missing as a dash", () => {
    expect(qualified("LT", "100", "ppm")).toBe("<100 ppm");
    expect(qualified("EQ", "0.0171")).toBe("0.0171");
    expect(qualified("GT", "5", "%")).toBe(">5 %");
    expect(qualified("EQ", null)).toBe("—");
    expect(reasonText("UNRESOLVED_CONFLICT")).toMatch(/no governing result/);
  });
});

describe("geo helpers", () => {
  it("builds closed uncertainty rings and selects by bbox", () => {
    const ring = circlePolygon(7.5, 9, 100).coordinates[0];
    expect(ring[0]).toEqual(ring[ring.length - 1]);
    expect(Math.abs(ring[0][0] - 7.5) * 111320 * Math.cos((9 * Math.PI) / 180)).toBeCloseTo(100, 0);
    const b = normaliseBBox([8, 10], [7, 9]);
    expect(b).toEqual([7, 9, 8, 10]);
    expect(inBBox([7.5, 9.5], b)).toBe(true);
    expect(inBBox([0, 0], b)).toBe(false);
  });
});

describe("mapping wizard guesses", () => {
  it("maps common headers and analyte columns without inventing units", () => {
    const g = guessColumns(["SampleID", "Easting", "Northing", "Certificate", "Li_ppm", "Ta2O5 (%)", "Sn", "Notes"]);
    expect(g.cols).toMatchObject({ sample_id: "SampleID", x: "Easting", y: "Northing", certificate_number: "Certificate" });
    expect(g.analytes).toEqual([{ column: "Li_ppm", analyte: "Li", unit: "ppm" }, { column: "Ta2O5 (%)", analyte: "Ta2O5", unit: "%" }]);
  });
});

describe("evidence items", () => {
  it("groups fields per anchor and keeps unlabelled anchors", () => {
    expect(evidenceItems({ sample_id: ["a"], result_raw: ["b"], unit: ["b"] }, ["a", "b", "c"])).toEqual([
      { anchorId: "a", field: "sample_id" },
      { anchorId: "b", field: "result_raw, unit" },
      { anchorId: "c", field: undefined },
    ]);
  });
});
