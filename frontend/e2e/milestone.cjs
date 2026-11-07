const { chromium } = require("playwright");
/*
 * Browser walkthrough of the FRD §13 first milestone against a running, freshly seeded server:
 *   cd backend && rm -rf var && python scripts/seed_demo.py && python -m ajgem serve   (after `npm run build`)
 *   cd frontend && npm i --no-save playwright@1.49.1 && npx playwright install chromium && node e2e/milestone.cjs
 * Screenshots are written to e2e/out/. Uses SYNTHETIC data only.
 */
const fs = require("fs");
const path = require("path");
const OUT = path.join(__dirname, "out");
const BASE = process.env.AJGEM_URL ?? "http://127.0.0.1:8000";
fs.mkdirSync(OUT, { recursive: true });
fs.writeFileSync(path.join(OUT, "new_assays.csv"), [
  "SampleID,Campaign,Easting,Northing,Certificate,Lab,Method,Basis,Li_ppm",
  "NW-001,NW-2026,336500,996200,CERT-077,Synthetic Lab,ICP-MS,dry,1420",
  "NW-002,NW-2026,336620,996310,CERT-077,Synthetic Lab,ICP-MS,dry,<50",
  "NW-003,NW-2026,,,CERT-077,Synthetic Lab,ICP-MS,dry,ppt",
].join("\n") + "\n");
const log = (...a) => console.log("✓", ...a);
(async () => {
  const b = await chromium.launch();
  const ctx = await b.newContext({ viewport: { width: 1440, height: 900 }, acceptDownloads: true });
  const page = await ctx.newPage();
  const errors = [];
  page.on("pageerror", (e) => errors.push("PAGEERROR " + e.message));
  page.on("console", (m) => m.type() === "error" && !/40[39]|422/.test(m.text()) && errors.push(m.text()));
  const shot = async (n) => { await page.waitForTimeout(400); await page.screenshot({ path: `${OUT}/flow-${n}.png` }); };
  const as = async (name) => {
    await page.goto(`${BASE}/login`);
    await page.getByRole("button", { name: new RegExp(`^${name}`) }).click();
    await page.waitForURL(/projects/);
  };
  const open = async (section) => {
    await page.goto(`${BASE}/projects`);
    await page.getByRole("link", { name: /Kano Pegmatite/ }).click();
    await page.waitForSelector(".tiles");
    if (section) await page.getByRole("navigation").getByRole("link", { name: section }).click();
  };

  // A. Analyst imports a CSV through the mapping wizard
  await as("Analyst");
  await open(/Upload/);
  await page.getByLabel("Choose file").setInputFiles(path.join(OUT, "new_assays.csv"));
  await page.getByPlaceholder(/Client report/).fill("Synthetic NW campaign table");
  await page.getByLabel(/I confirm I have the right/).check();
  await page.getByRole("button", { name: "Upload", exact: true }).click();
  await page.waitForSelector("text=Map columns");
  if ((await page.getByLabel("Sample ID *").inputValue()) !== "SampleID") throw new Error("sample_id not guessed");
  await page.getByPlaceholder(/EPSG:32632 or/).fill("EPSG:32632");
  await page.getByLabel(/I have confirmed this CRS/).check();
  await page.getByRole("button", { name: "Preview & validate" }).click();
  await page.waitForSelector("text=Critical issues");
  await shot("a-preview");
  await page.getByRole("button", { name: /Create \d+ candidates/ }).click();
  await page.waitForSelector("text=Imported as candidates");
  log("analyst imported CSV via wizard");

  // B. Reviewer1 approves NW-001 and revised KW-001, corrects KW-103
  await as("Reviewer1");
  await open(/^Review/);
  await page.getByRole("button", { name: "assay", exact: true }).click();
  for (const t of ["NW-001 · Li", "KW-001 · Li"]) {
    await page.locator(".queue-item", { hasText: t }).click();
    await page.getByRole("button", { name: /Approve revision 1/ }).click();
    await page.waitForTimeout(700);
    if (await page.locator(".queue-item", { hasText: t }).count()) throw new Error(`${t} still queued`);
    log("reviewer1 approved", t);
  }
  await page.locator(".queue-item", { hasText: "NW-003 · Li" }).click();
  if (!(await page.getByRole("button", { name: /Approve revision/ }).isDisabled())) throw new Error("critical issue did not block approval");
  log("critical issue blocks approval for NW-003");
  await page.locator(".queue-item", { hasText: "KW-103 · Li" }).click();
  await page.locator("#f-result_raw").fill("604");
  await page.getByPlaceholder(/Decimal point lost/).fill("Digits transposed in extraction; certificate page 8 reads 604");
  await page.getByRole("button", { name: "Save correction" }).click();
  await page.waitForSelector("text=Correction saved");
  await page.getByLabel(/Hide revisions I authored/).uncheck();
  await page.locator(".queue-item", { hasText: "KW-103 · Li" }).click();
  await page.waitForSelector("text=You authored this revision");
  if (!(await page.getByRole("button", { name: /Approve revision 2/ }).isDisabled())) throw new Error("self-approval not blocked");
  await shot("b-self-approval");
  log("correction saved; self-approval blocked in UI");

  // C. Reviewer2 approves the correction
  await as("Reviewer2");
  await open(/^Review/);
  await page.locator(".queue-item", { hasText: "KW-103 · Li" }).click();
  await page.getByRole("button", { name: /Approve revision 2/ }).click();
  await page.waitForTimeout(700);
  log("reviewer2 approved correction");

  // D. Reviewer1 resolves the conflict
  await as("Reviewer1");
  await open(/Conflicts/);
  await page.getByLabel("Select CERT-001-R1 as governing").check();
  await page.getByPlaceholder(/re-assay of pulp/).fill("Laboratory reissued the certificate after re-assay of the pulp; R1 governs.");
  await page.getByRole("button", { name: "Select governing result" }).click();
  await page.getByRole("button", { name: /^All/ }).click();
  await page.waitForSelector("text=resolved");
  await shot("d-resolved");
  log("conflict resolved with rationale");

  // E. Analyst: stale answer, fresh answer, export
  await as("Analyst");
  await open(/Assistant/);
  await page.locator("td .badge", { hasText: "stale" }).first().waitFor({ timeout: 5000 });
  log("earlier answer shows as stale");
  await page.getByRole("button", { name: "Which samples exceed 1000 ppm Li?" }).click();
  await page.waitForSelector("text=Excluded from the calculation");
  const yes = await page.locator("table.data tbody tr", { hasText: "NW-001" }).count();
  const kw1 = await page.locator("table.data tbody tr", { hasText: "KW-001" }).first().innerText();
  if (!yes) throw new Error("NW-001 missing from result");
  if (!/Superseded by the governing/.test(kw1)) throw new Error("KW-001 not excluded as superseded: " + kw1);
  await shot("e-answer");
  log("fresh answer: NW-001 meets; KW-001 1,200 superseded by governing 900");
  await page.locator("table.data tbody tr", { hasText: "NW-001" }).getByRole("button", { name: /sources/ }).click();
  await page.waitForSelector(".drawer >> text=Raw value");
  await shot("e-evidence");
  await page.keyboard.press("Escape");
  await page.getByRole("navigation").getByRole("link", { name: /Jobs/ }).click();
  await page.getByPlaceholder(/Quarterly technical/).fill("Milestone provenance check");
  await page.getByRole("button", { name: "Create export" }).click();
  await page.waitForSelector("text=Manifest ·");
  const [dl] = await Promise.all([page.waitForEvent("download"), page.getByRole("button", { name: "Download ZIP" }).click()]);
  log("export downloaded:", dl.suggestedFilename());
  await shot("e-export");

  // F. Outsider sees nothing
  await as("Outsider");
  if (await page.getByRole("link", { name: /Kano/ }).count()) throw new Error("outsider sees project");
  const pid = (await (await ctx.request.get(`${BASE}/api/projects`, { headers: { "X-User-Id": await page.evaluate(() => localStorage.getItem("ajgem.devUserId")) } })).json()).projects.length;
  if (pid !== 0) throw new Error("outsider API lists projects");
  log("outsider sees no projects");
  if (errors.length) throw new Error("Page errors: " + JSON.stringify(errors));
  console.log("Milestone walkthrough passed.");
  await b.close();
})().catch(async (e) => { console.error("FAIL", e.message); process.exit(1); });
