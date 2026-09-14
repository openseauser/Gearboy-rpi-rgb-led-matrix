// CDP collector: per requestId event order + Network.getResponseBody outcome, per case.
import { chromium } from "playwright-core";
import { start } from "./server.mjs";
const BIN = "/opt/pw-browsers/chromium-1194/chrome-linux/chrome";
const PORT = 8089, O = `http://127.0.0.1:${PORT}`;
const srv = await start(PORT);
const browser = await chromium.launch({ executablePath: BIN });
const ctx = await browser.newContext();
const page = await ctx.newPage();
const cdp = await ctx.newCDPSession(page);
await cdp.send("Network.enable");
const reqs = new Map(); // requestId -> record
const ev = (name) => cdp.on(name, (p) => {
  const r = reqs.get(p.requestId) ?? { id: p.requestId, events: [], url: null };
  reqs.set(p.requestId, r);
  const e = { name, ts: p.timestamp };
  if (name === "Network.requestWillBeSent") { e.url = p.request.url; e.redirectResponse = p.redirectResponse ? p.redirectResponse.status : undefined; r.url = r.url ?? p.request.url; r.finalUrl = p.request.url; r.type = p.type; }
  if (name === "Network.responseReceived") { e.status = p.response.status; e.fromDiskCache = p.response.fromDiskCache; e.fromServiceWorker = p.response.fromServiceWorker; e.fromPrefetchCache = p.response.fromPrefetchCache; e.mime = p.response.mimeType; r.status = p.response.status; r.fromDiskCache = p.response.fromDiskCache; r.fromServiceWorker = p.response.fromServiceWorker; r.mime = p.response.mimeType; }
  if (name === "Network.loadingFinished") { e.encodedDataLength = p.encodedDataLength; r.finished = true; }
  if (name === "Network.loadingFailed") { e.errorText = p.errorText; e.canceled = p.canceled; e.blockedReason = p.blockedReason; r.failed = { errorText: p.errorText, canceled: p.canceled }; }
  if (name === "Network.dataReceived") { r.dataBytes = (r.dataBytes ?? 0) + p.dataLength; }
  if (name === "Network.requestServedFromCache") { r.servedFromCache = true; }
  r.events.push(e);
});
for (const n of ["Network.requestWillBeSent","Network.requestServedFromCache","Network.responseReceived","Network.dataReceived","Network.loadingFinished","Network.loadingFailed"]) ev(n);

async function getBody(id) {
  try { const b = await cdp.send("Network.getResponseBody", { requestId: id }); return { ok: true, len: b.body.length, base64: b.base64Encoded }; }
  catch (e) { return { ok: false, err: e.message.replace(/^Protocol error \([^)]*\): /, "").split("\n")[0] }; }
}
const results = [];
async function runCase(name, fn, opts = {}) {
  reqs.clear();
  await fn();
  await page.waitForTimeout(opts.settle ?? 400);
  for (const r of reqs.values()) {
    if (opts.filter && !opts.filter(r)) continue;
    const immediate = await getBody(r.id);
    results.push({ case: name, id: r.id, url: (r.finalUrl ?? r.url ?? "").replace(O, ""), type: r.type, status: r.status, fromDiskCache: r.fromDiskCache, fromSW: r.fromServiceWorker, servedFromCache: r.servedFromCache ?? false, finished: !!r.finished, failed: r.failed, dataBytes: r.dataBytes ?? 0, events: r.events.map(e => e.name.replace("Network.", "") + (e.redirectResponse ? `(redirect ${e.redirectResponse})` : "") + (e.status ? `(${e.status})` : "") + (e.errorText ? `(${e.errorText}${e.canceled ? ",canceled" : ""})` : "")).join(" > "), body: immediate });
  }
}
const fetchIn = (path, init) => page.evaluate(([p, i]) => fetch(p, i).then(r => r.text()).catch(e => "ERR:" + e.message), [path, init ?? {}]);
await page.goto(O + "/");
await runCase("200 json", () => fetchIn("/json"));
await runCase("200 empty body", () => fetchIn("/empty200"));
await runCase("204 no content", () => fetchIn("/204"));
await runCase("500 json", () => fetchIn("/err500"));
await runCase("404 json", () => fetchIn("/nope"));
await runCase("etag first (200)", () => fetchIn("/etag"));
await runCase("etag revalidate (304)", () => fetchIn("/etag"));
await runCase("cached first (200)", () => fetchIn("/cached"));
await runCase("cached second (memory/disk)", () => fetchIn("/cached"));
await runCase("redirect chain 301>302>200", () => fetchIn("/r1"));
await runCase("data: URL", () => fetchIn("data:application/json,%7B%22d%22%3A1%7D"));
await runCase("connection reset", () => fetchIn("/reset"));
await runCase("abort via AbortController mid-body", () => page.evaluate(() => { const c = new AbortController(); const p = fetch("/slow", { signal: c.signal }).then(r => r.text()).catch(e => "ERR:" + e.name); setTimeout(() => c.abort(), 300); return p; }), { settle: 800 });
await runCase("navigation cancels in-flight fetch", async () => { page.evaluate(() => fetch("/slow").then(r => r.text()).catch(() => {})); await page.waitForTimeout(200); await page.goto(O + "/"); }, { settle: 800, filter: r => (r.url ?? "").includes("/slow") });
// service worker
await page.evaluate(async () => { await navigator.serviceWorker.register("/sw.js"); await navigator.serviceWorker.ready; });
await page.reload(); await page.waitForTimeout(300);
await runCase("sw synthesized response", () => fetchIn("/sw-synth"));
await runCase("sw pass-through fetch", () => fetchIn("/sw-pass"));
// eviction: getResponseBody after many later requests (buffer eviction) and after navigation
reqs.clear(); await fetchIn("/json"); await page.waitForTimeout(200); const keep = [...reqs.values()][0];
for (let i = 0; i < 60; i++) await fetchIn("/json?i=" + i);
results.push({ case: "body after 60 later requests (same page)", id: keep.id, url: "/json", status: keep.status, finished: !!keep.finished, events: "…", body: await getBody(keep.id) });
reqs.clear(); await fetchIn("/json"); await page.waitForTimeout(200); const keep2 = [...reqs.values()][0];
await page.goto(O + "/"); await page.waitForTimeout(200);
results.push({ case: "body after same-origin navigation", id: keep2.id, url: "/json", status: keep2.status, finished: !!keep2.finished, events: "…", body: await getBody(keep2.id) });
// detached target: open popup, fetch there via its own session, close it, then ask
const popup = await ctx.newPage(); const s2 = await ctx.newCDPSession(popup); await s2.send("Network.enable"); let pid = null; s2.on("Network.loadingFinished", p => pid = p.requestId); await popup.goto(O + "/"); await popup.evaluate(() => fetch("/json").then(r => r.text())); await popup.waitForTimeout(300);
let detached; try { const b = await s2.send("Network.getResponseBody", { requestId: pid }); detached = { before_close: { ok: true, len: b.body.length } }; } catch (e) { detached = { before_close: { ok: false, err: e.message } }; }
await popup.close(); try { const b = await s2.send("Network.getResponseBody", { requestId: pid }); detached.after_close = { ok: true, len: b.body.length }; } catch (e) { detached.after_close = { ok: false, err: e.message.split("\n")[0] }; }
results.push({ case: "target closed (popup) then getResponseBody", id: pid, events: "…", body: detached });
console.log(JSON.stringify({ chromium: browser.version(), results }, null, 1));
await browser.close(); srv.close();
