const { chromium } = require("playwright");
/*
 * NF12 accessibility check: runs axe-core (WCAG 2.1 A/AA rules) on every screen of a running, seeded server, in light
 * and dark colour schemes, and fails on any serious or critical violation. Needs `npm i --no-save axe-core`.
 *   node e2e/a11y.cjs            # writes e2e/out/a11y.json with every finding, including minor ones
 */
const fs = require("fs");
const path = require("path");
const OUT = path.join(__dirname, "out");
const BASE = process.env.AJGEM_URL ?? "http://127.0.0.1:8000";
const AXE = fs.readFileSync(require.resolve("axe-core/axe.min.js"), "utf8");
fs.mkdirSync(OUT, { recursive: true });

const SCREENS = [
  ["Dashboard", null], ["Upload & import", "Upload & import"], ["Review", "Review"], ["Data explorer", "Data explorer"],
  ["Map", "Map"], ["Assistant", "Assistant"], ["Conflicts & versions", "Conflicts & versions"], ["Targets", "Targets"],
  ["Jobs & exports", "Jobs & exports"], ["Administration", "Administration"],
];

(async () => {
  const b = await chromium.launch();
  const report = [];
  let blocking = 0;
  for (const scheme of ["light", "dark"]) {
    // The app's CSP (correctly) blocks injected scripts; the checker is injected by the test harness only.
    const ctx = await b.newContext({ viewport: { width: 1440, height: 1000 }, colorScheme: scheme, bypassCSP: true });
    const page = await ctx.newPage();
    const scan = async (name) => {
      await page.waitForTimeout(800);
      await page.addScriptTag({ content: AXE });
      const res = await page.evaluate(async () => (await window.axe.run(document, {
        runOnly: { type: "tag", values: ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"] },
        resultTypes: ["violations"],
      })).violations.map((v) => ({ id: v.id, impact: v.impact, help: v.help, nodes: v.nodes.slice(0, 5).map((n) => n.target.join(" ")) })));
      for (const v of res) {
        report.push({ scheme, screen: name, ...v });
        if (v.impact === "serious" || v.impact === "critical") {
          blocking += 1;
          console.log(`✗ [${scheme}] ${name}: ${v.id} (${v.impact}) ${v.help}\n    ${v.nodes.join("\n    ")}`);
        }
      }
      if (!res.some((v) => v.impact === "serious" || v.impact === "critical")) console.log(`✓ [${scheme}] ${name}`);
    };
    await page.goto(`${BASE}/login`);
    await scan("Sign-in");
    // Reviewers see every screen except upload; analysts see upload. Scan as each where relevant.
    for (const who of ["Admin", "Reviewer", "Analyst"]) {
      await page.goto(`${BASE}/login`);
      await page.getByRole("button", { name: new RegExp(`^${who}`) }).first().click();
      await page.waitForURL(/projects/);
      if (who === "Admin") await scan("Projects");
      await page.getByRole("link", { name: /Kano Pegmatite/ }).click();
      await page.waitForSelector(".tiles");
      for (const [name, link] of SCREENS) {
        const nav = page.getByRole("navigation").getByRole("link", { name: link ?? "Dashboard", exact: true });
        if (!(await nav.count())) continue;
        await nav.click();
        await page.waitForLoadState("networkidle");
        await scan(`${name} (${who.toLowerCase()})`);
      }
    }
    await ctx.close();
  }
  fs.writeFileSync(path.join(OUT, "a11y.json"), JSON.stringify(report, null, 1));
  await b.close();
  if (blocking) {
    console.error(`FAIL ${blocking} serious or critical accessibility violation(s); details in e2e/out/a11y.json`);
    process.exit(1);
  }
  console.log(`Accessibility check passed (${report.length} minor or moderate finding(s) recorded in e2e/out/a11y.json).`);
})().catch((e) => { console.error("FAIL", e.message); process.exit(1); });
