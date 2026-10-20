// Screenshots of the main views in desktop, landscape tablet and phone sizes.
// Usage: SHOT_URL=http://localhost:8080 SHOT_PASSWORD=... OUT=docs/screenshots node scripts/screenshots.mjs
// Needs `npm i --no-save playwright`. Run against demo mode (APP_MODE=demo) so no routing requests are made.
import { chromium } from "playwright";
import fs from "node:fs";

const url = process.env.SHOT_URL || "http://localhost:8080";
const pw = process.env.SHOT_PASSWORD || "";
const out = process.env.OUT || "docs/screenshots";
const exe = process.env.CHROMIUM_PATH || undefined;
fs.mkdirSync(out, { recursive: true });

const sizes = {
  desktop: { width: 1440, height: 900 },
  tablet: { width: 1280, height: 800 },
  mobile: { width: 390, height: 844, isMobile: true, deviceScaleFactor: 2 },
};
const views = ["map", "map-history", "analysis", "plan", "setup", "budget"];
const only = (process.env.ONLY || "").split(",").filter(Boolean);

const browser = await chromium.launch(exe ? { executablePath: exe } : {});
const errors = [];
for (const [name, vp] of Object.entries(sizes)) {
  const ctx = await browser.newContext({ viewport: { width: vp.width, height: vp.height }, isMobile: !!vp.isMobile, deviceScaleFactor: vp.deviceScaleFactor || 1, timezoneId: "Europe/Amsterdam" });
  const page = await ctx.newPage();
  page.on("pageerror", e => errors.push(`${name}: ${e.message}`));
  page.on("console", m => { if (m.type() === "error") errors.push(`${name}: console ${m.text()}`); });
  await page.goto(url + "/");
  if (await page.locator("#login:not(.hidden)").count()) {
    await page.fill("#login-pw", pw);
    await page.click("#login-form button[type=submit]");
    await page.waitForLoadState("networkidle");
  }
  for (const v of views) {
    if (only.length && !only.includes(v)) continue;
    const [hash, sub] = v.split("-");
    await page.goto(url + "/#" + hash);
    await page.waitForLoadState("networkidle");
    if (sub === "history") { await page.click("#m-history"); await page.waitForLoadState("networkidle"); }
    if (hash === "plan") { await page.click("#p-go"); await page.waitForLoadState("networkidle"); }
    await page.waitForTimeout(700);
    await page.screenshot({ path: `${out}/${name}-${v}.png`, fullPage: hash !== "map" });
  }
  if (name === "tablet" && (!only.length || only.includes("dashboard"))) {
    await page.goto(url + "/dashboard");
    await page.waitForLoadState("networkidle");
    await page.waitForTimeout(800);
    await page.screenshot({ path: `${out}/tablet-dashboard.png` });
  }
  await ctx.close();
}
await browser.close();
if (errors.length) { console.error(errors.join("\n")); process.exitCode = 1; }
else console.log("screenshots written to", out);
