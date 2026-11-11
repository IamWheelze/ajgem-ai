const { chromium } = require("playwright");
/*
 * Browser walkthrough of FR18 targets and checklists against a running, seeded server (run after milestone.cjs
 * or on a fresh seed): a reviewer saves a checklist, an analyst creates a target, links reviewed evidence as
 * supporting and contrary, and the summary shows checklist gaps without any generated support.
 * Uses SYNTHETIC data only. Screenshots go to e2e/out/.
 */
const fs = require("fs");
const path = require("path");
const OUT = path.join(__dirname, "out");
const BASE = process.env.AJGEM_URL ?? "http://127.0.0.1:8000";
fs.mkdirSync(OUT, { recursive: true });
const log = (...a) => console.log("✓", ...a);

(async () => {
  const b = await chromium.launch();
  const ctx = await b.newContext({ viewport: { width: 1440, height: 1000 } });
  const page = await ctx.newPage();
  const errors = [];
  page.on("pageerror", (e) => errors.push("PAGEERROR " + e.message));
  page.on("console", (m) => m.type() === "error" && !/40[39]|422/.test(m.text()) && errors.push(m.text()));
  page.on("dialog", (d) => d.accept("Linked to the wrong dyke"));
  const as = async (name) => {
    await page.goto(`${BASE}/login`);
    await page.getByRole("button", { name: new RegExp(`^${name}`) }).first().click();
    await page.waitForURL(/projects/);
    await page.getByRole("link", { name: /Kano Pegmatite/ }).click();
    await page.waitForSelector(".tiles");
    await page.getByRole("navigation").getByRole("link", { name: "Targets" }).click();
    await page.waitForSelector("h1:has-text('Targets')");
  };
  const suffix = Date.now().toString(36);

  // A. Reviewer saves a checklist version.
  await as("Reviewer");
  await page.getByLabel("Checklist name").fill(`Li screening ${suffix}`);
  await page.getByLabel(/Items, one per line/).fill("host-rock: Host pegmatite mapped\nli-assay: Li assay above cut-off reported\nmineralogy: Li mineral identified");
  await page.getByRole("button", { name: "Save checklist version" }).click();
  await page.getByRole("button", { name: `Li screening ${suffix} v1` }).waitFor();
  log("reviewer saved checklist v1");

  // B. Analyst creates a target that uses it; the empty state explains itself.
  await as("Analyst");
  await page.getByLabel("New target name").fill(`Dyke D7 ${suffix}`);
  await page.getByLabel("Description").fill("NE-trending pegmatite (synthetic)");
  await page.getByLabel("Checklist", { exact: true }).selectOption({ label: `Li screening ${suffix} v1` });
  await page.getByRole("button", { name: "Create target" }).click();
  await page.getByText("does not generate geological support").waitFor();
  if ((await page.getByText("missing evidence").count()) !== 3) throw new Error("expected 3 missing checklist items");
  log("empty target explains itself; 3 checklist items missing");

  // C. Link KW-101 as supporting (addresses li-assay) and KW-102 as contrary.
  const linkEvidence = async (sid, stance, item) => {
    await page.getByLabel("Sample ID or certificate number").fill(sid);
    await page.getByRole("button", { name: "Find approved records" }).click();
    await page.locator(".chip", { hasText: new RegExp(`${sid} · Li`) }).first().click();
    await page.getByRole("group", { name: "Stance" }).getByRole("button", { name: stance }).click();
    if (item) await page.getByLabel(item).check();
    await page.getByLabel("Note").fill(`${stance} evidence from the synthetic certificate`);
    await page.getByRole("button", { name: new RegExp(`^Link as ${stance.toLowerCase()}`) }).click();
    await page.getByRole("heading", { name: new RegExp(`^${stance} · 1`) }).waitFor();
  };
  await linkEvidence("KW-001", "Supporting", "Li assay above cut-off reported");
  await linkEvidence("KW-002", "Contrary", null);
  if ((await page.getByText("missing evidence").count()) !== 2) throw new Error("expected 2 missing items after linking");
  await page.screenshot({ path: path.join(OUT, "targets-summary.png"), fullPage: true });
  log("linked supporting and contrary evidence; li-assay addressed, 2 items still missing");

  // D. The evidence drawer opens the source of a linked record.
  await page.getByRole("button", { name: "Evidence" }).first().click();
  await page.getByText(/sha256/i).first().waitFor();
  await page.keyboard.press("Escape");
  log("linked evidence opens its source");

  // E. Removing a link asks for a reason and updates the summary.
  await page.getByRole("button", { name: "Remove" }).last().click();
  await page.getByRole("heading", { name: /^Contrary · 0/ }).waitFor();
  log("link removed with a reason");

  // F. A viewer can read the target but not change it.
  await as("Viewer");
  await page.getByRole("row", { name: new RegExp(`Dyke D7 ${suffix}`) }).click();
  await page.getByRole("heading", { name: /^Supporting · 1/ }).waitFor();
  if (await page.getByRole("button", { name: "Create target" }).count()) throw new Error("viewer can create targets");
  if (await page.getByRole("button", { name: "Remove" }).count()) throw new Error("viewer can remove links");
  log("viewer reads but cannot edit");

  if (errors.length) throw new Error("Page errors: " + JSON.stringify(errors));
  console.log("Targets walkthrough passed.");
  await b.close();
})().catch(async (e) => { console.error("FAIL", e.message); process.exit(1); });
