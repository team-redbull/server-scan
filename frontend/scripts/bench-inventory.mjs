// Inventory-page UX benchmark (ADR-0033): one operator session against a
// running build, then a 60 s idle window, N runs, medians to stdout and
// bench-<label>.json to the working directory.
//   node frontend/scripts/bench-inventory.mjs http://localhost:4174 after 5
// Needs the API on :8080 with a seeded fleet and the build served with the
// /api proxy (`vite preview`); in this WSL sandbox, LD_LIBRARY_PATH as in
// the Playwright memory note.
import { chromium } from "@playwright/test";
import fs from "node:fs";
const [base, label, runsArg] = process.argv.slice(2);
const runs = Number(runsArg ?? 5);
const firstRow = (page) => page.locator("tbody tr td a").first();
const firstName = (page) => firstRow(page).textContent();
async function until(page, pred, timeout = 15000) {
  const t0 = performance.now();
  await page.waitForFunction(pred.fn, pred.arg, { polling: 5, timeout });
  return performance.now() - t0;
}
const changed = (sel, prev) => ({ fn: ([s, p]) => { const el = document.querySelector(s); return el && el.textContent !== p; }, arg: [sel, prev] });
const results = [];
const browser = await chromium.launch({ args: ["--enable-precise-memory-info"] });
for (let i = 0; i < runs; i++) {
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 } });
  const page = await ctx.newPage();
  const cdp = await ctx.newCDPSession(page);
  await cdp.send("Network.enable");
  let bytes = 0, reqs = 0, apiReqs = 0, apiBytes = 0; const urls = new Map();
  cdp.on("Network.requestWillBeSent", (e) => { urls.set(e.requestId, e.request.url); reqs++; if (e.request.url.includes("/api/")) apiReqs++; });
  cdp.on("Network.loadingFinished", (e) => { bytes += e.encodedDataLength; if ((urls.get(e.requestId) ?? "").includes("/api/")) apiBytes += e.encodedDataLength; });
  const r = { run: i + 1 };
  let t = performance.now();
  await page.goto(`${base}/servers`);
  await firstRow(page).waitFor();
  r.cold_load_ms = performance.now() - t;
  r.load_bytes = bytes; r.load_reqs = reqs;
  // filter: vendor -> dell
  let prev = await firstName(page); let heading = await page.locator("main p").first().textContent();
  t = performance.now(); await page.selectOption("#filter-vendor", "dell");
  await until(page, { fn: ([p, h]) => { const a = document.querySelector("tbody tr td a"); const hp = document.querySelector("main p"); return a && hp && (a.textContent !== p || hp.textContent !== h); }, arg: [prev, heading] });
  r.filter_vendor_ms = performance.now() - t;
  // second filter: health -> CRITICAL
  prev = await firstName(page); heading = await page.locator("main p").first().textContent();
  t = performance.now(); await page.selectOption("#filter-health", "CRITICAL").catch(async () => page.locator("select").filter({ has: page.locator("option[value=CRITICAL]") }).first().selectOption("CRITICAL"));
  await until(page, { fn: ([p, h]) => { const a = document.querySelector("tbody tr td a"); const hp = document.querySelector("main p"); return (a ? a.textContent !== p : true) || (hp && hp.textContent !== h); }, arg: [prev, heading] });
  r.filter_health_ms = performance.now() - t;
  // clear filters
  const clear = page.getByRole("button", { name: /clear/i });
  if (await clear.count()) { prev = await firstName(page); t = performance.now(); await clear.first().click(); await until(page, changed("tbody tr td a", prev)); r.clear_ms = performance.now() - t; }
  // next page
  prev = await firstName(page); t = performance.now();
  await page.getByRole("button", { name: /next/i }).first().click();
  await until(page, changed("tbody tr td a", prev)); r.next_page_ms = performance.now() - t;
  // sort by model (click header)
  prev = await firstName(page); t = performance.now();
  await page.getByRole("columnheader", { name: /model/i }).first().click().catch(() => page.getByText("Model", { exact: true }).first().click());
  await until(page, changed("tbody tr td a", prev)); r.sort_model_ms = performance.now() - t;
  // search (includes the 300ms debounce in both designs)
  heading = await page.locator("main p").first().textContent(); t = performance.now();
  await page.getByPlaceholder(/Name, serial/i).fill("cisco-m6");
  await until(page, changed("main p", heading)); r.search_ms = performance.now() - t;
  r.session_bytes = bytes; r.session_reqs = reqs; r.api_reqs = apiReqs; r.api_bytes = apiBytes;
  // wall-display idle: 60 s open, count traffic
  const b0 = bytes, q0 = apiReqs; await page.waitForTimeout(60000);
  r.idle60s_api_reqs = apiReqs - q0; r.idle60s_bytes = bytes - b0;
  r.heap_mb = await page.evaluate(() => Math.round((performance.memory?.usedJSHeapSize ?? 0) / 1e6));
  results.push(r); await ctx.close();
}
await browser.close();
const keys = Object.keys(results[0]).filter((k) => k !== "run");
const med = (k) => { const v = results.map((r) => r[k]).filter((x) => typeof x === "number").sort((a, b) => a - b); return v[Math.floor(v.length / 2)]; };
const summary = Object.fromEntries(keys.map((k) => [k, med(k)]));
console.log(label, JSON.stringify(summary, null, 1));
fs.writeFileSync(`${process.cwd()}/bench-${label}.json`, JSON.stringify({ label, base, runs, results, median: summary }, null, 1));
