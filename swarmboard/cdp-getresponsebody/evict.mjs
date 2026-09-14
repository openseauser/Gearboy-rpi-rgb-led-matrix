import { chromium } from "playwright-core";
import http from "node:http";
const BIN = "/opt/pw-browsers/chromium-1194/chrome-linux/chrome";
const PORT = 8090, O = `http://127.0.0.1:${PORT}`;
const big = JSON.stringify({ pad: "x".repeat(1024 * 1024) }); // ~1 MiB
const ETAG = '"v1"';
const srv = http.createServer((req, res) => {
  const p = new URL(req.url, "http://x").pathname;
  if (p === "/") return (res.writeHead(200, { "content-type": "text/html" }), res.end("<p>ok</p>"));
  if (p === "/etag") { if (req.headers["if-none-match"] === ETAG) { res.writeHead(304, { etag: ETAG }); return res.end(); } res.writeHead(200, { "content-type": "application/json", etag: ETAG, "cache-control": "no-cache" }); return res.end('{"a":1}'); }
  res.writeHead(200, { "content-type": "application/json", "cache-control": "no-store" }); res.end(big);
});
await new Promise(r => srv.listen(PORT, "127.0.0.1", r));
const browser = await chromium.launch({ executablePath: BIN });
async function scenario(label, enableParams, n) {
  const ctx = await browser.newContext(); const page = await ctx.newPage(); const cdp = await ctx.newCDPSession(page);
  await cdp.send("Network.enable", enableParams);
  const finished = []; cdp.on("Network.loadingFinished", p => finished.push(p.requestId));
  const extra = new Map(); cdp.on("Network.responseReceivedExtraInfo", p => extra.set(p.requestId, p.statusCode));
  const rr = new Map(); cdp.on("Network.responseReceived", p => rr.set(p.requestId, p.response.status));
  await page.goto(O + "/");
  await page.evaluate(() => fetch("/etag").then(r => r.text())); await page.waitForTimeout(150);
  await page.evaluate(() => fetch("/etag").then(r => r.text())); await page.waitForTimeout(150);
  const etagIds = [...rr.keys()].filter(id => extra.has(id));
  for (let i = 0; i < n; i++) await page.evaluate(i => fetch("/big?i=" + i).then(r => r.text()), i);
  await page.waitForTimeout(300);
  const bigIds = finished.filter(id => !etagIds.includes(id));
  const probe = async id => { try { const b = await cdp.send("Network.getResponseBody", { requestId: id }); return "ok(" + b.body.length + ")"; } catch (e) { return "ERR: " + e.message.replace(/^.*\(Network.getResponseBody\): /, ""); } };
  console.log(`== ${label}: Network.enable(${JSON.stringify(enableParams)}), ${n} x 1 MiB bodies, all loadingFinished (${bigIds.length}) ==`);
  console.log("  304 revalidation: responseReceived.status=" + rr.get(etagIds[1]) + ", responseReceivedExtraInfo.statusCode=" + extra.get(etagIds[1]) + ", body=" + await probe(etagIds[1]));
  for (const k of [0, 1, Math.floor(n / 2), n - 2, n - 1]) console.log(`  big #${k}: ${await probe(bigIds[k])}`);
  await ctx.close();
}
await scenario("A default buffers", {}, 40);
await scenario("B maxTotalBufferSize=3MiB, maxResourceBufferSize=2MiB", { maxTotalBufferSize: 3 * 1024 * 1024, maxResourceBufferSize: 2 * 1024 * 1024 }, 8);
await scenario("C maxResourceBufferSize=512KiB (each body larger than the per-resource cap)", { maxTotalBufferSize: 50 * 1024 * 1024, maxResourceBufferSize: 512 * 1024 }, 3);
await browser.close(); srv.close();
