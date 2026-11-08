const { chromium } = require("playwright");
/*
 * Browser check of OIDC sign-in (NF02) against the DEVELOPMENT-ONLY mock provider:
 *   cd backend && rm -rf var && python scripts/seed_demo.py
 *   python scripts/mock_oidc.py &
 *   AJGEM_AUTH_MODE=oidc AJGEM_OIDC_ISSUER=http://127.0.0.1:9000 AJGEM_OIDC_CLIENT_ID=ajgem-web python -m ajgem serve
 *   cd frontend && node e2e/oidc.cjs
 */
const path = require("path");
const OUT = path.join(__dirname, "out");
require("fs").mkdirSync(OUT, { recursive: true });
const ok = (cond, msg) => { if (!cond) throw new Error("expected: " + msg); console.log("✓", msg); };
(async () => {
  const b = await chromium.launch();
  const ctx = await b.newContext({ viewport: { width: 1440, height: 950 }, deviceScaleFactor: 2 });
  const page = await ctx.newPage();
  const errors = [];
  page.on("pageerror", (e) => errors.push(e.message));
  const signIn = async (email, mfa) => {
    await page.goto("http://127.0.0.1:8000/login");
    await page.getByRole("button", { name: "Sign in with your organisation" }).click();
    await page.waitForURL(/127\.0\.0\.1:9000\/authorize/);
    await page.fill("input[name=email]", email);
    if (mfa) await page.check("input[name=mfa]");
    await page.getByRole("button", { name: "Sign in" }).click();
    await page.waitForURL(/\/projects$/);
  };
  await signIn("reviewer2@kano.example", false);
  console.log("✓ signed in via mock IdP (code + PKCE), landed on /projects");
  await page.getByRole("link", { name: /Kano/ }).click();
  await page.waitForSelector(".tiles");
  console.log("✓ sidebar:", (await page.locator(".side-foot").innerText()).replace(/\n/g, " | "));
  await page.getByRole("navigation").getByRole("link", { name: /^Review/ }).click();
  await page.locator(".queue-item", { hasText: "KW-101 · Li" }).click();
  await page.getByRole("button", { name: /Approve revision 1/ }).click();
  await page.waitForSelector("text=MFA REQUIRED");
  await page.screenshot({ path: path.join(OUT, "mfa-required.png") });
  console.log("✓ approval without MFA refused with MFA_REQUIRED");
  // The dev header must not work in OIDC mode.
  const r = await page.evaluate(async () => (await fetch("/api/me", { headers: { "X-User-Id": "anything" } })).status);
  ok(r === 401, "the development X-User-Id header is ignored in OIDC mode");
  await page.getByRole("button", { name: "Sign in again with your second factor" }).click();
  await page.waitForURL(/\/login/);
  await signIn("reviewer2@kano.example", true);
  await page.getByRole("link", { name: /Kano/ }).click();
  await page.waitForSelector(".tiles");
  await page.getByRole("navigation").getByRole("link", { name: /^Review/ }).click();
  await page.locator(".queue-item", { hasText: "KW-101 · Li" }).click();
  await page.getByRole("button", { name: /Approve revision 1/ }).click();
  await page.waitForTimeout(800);
  const still = await page.locator(".queue-item", { hasText: "KW-101 · Li" }).count();
  ok(still === 0, "with MFA the approval succeeds");
  await page.screenshot({ path: path.join(OUT, "mfa-ok.png") });
  // Unregistered identity is refused.
  await page.getByRole("button", { name: "Sign out" }).click();
  await page.waitForURL(/\/login/);
  await page.getByRole("button", { name: "Sign in with your organisation" }).click();
  await page.fill("input[name=email]", "stranger@nowhere.test");
  await page.getByRole("button", { name: "Sign in" }).click();
  await page.waitForSelector("text=not registered", { timeout: 10000 });
  ok(new URL(page.url()).pathname === "/login", "an unregistered identity is signed out with an explanation");
  if (errors.length) throw new Error("Page errors: " + JSON.stringify(errors));
  console.log("OIDC walkthrough passed.");
  await b.close();
})().catch((e) => { console.error("FAIL", e.message); process.exit(1); });
