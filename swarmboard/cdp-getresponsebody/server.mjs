// Fixture server for CDP response-capture cases. No external data; everything synthetic.
import http from "node:http";
const JSON_BODY = JSON.stringify({ ok: true, n: 1 });
const ETAG = '"v1"';
const sw = `self.addEventListener('install',e=>self.skipWaiting());self.addEventListener('activate',e=>e.waitUntil(self.clients.claim()));
self.addEventListener('fetch',e=>{const u=new URL(e.request.url);
 if(u.pathname==='/sw-synth') e.respondWith(new Response(JSON.stringify({from:'sw'}),{headers:{'content-type':'application/json'}}));
 else if(u.pathname==='/sw-pass') e.respondWith(fetch(e.request));
});`;
export function start(port) {
  const srv = http.createServer((req, res) => {
    const u = new URL(req.url, "http://x");
    const p = u.pathname;
    const json = (code, body, extra = {}) => { res.writeHead(code, { "content-type": "application/json", "cache-control": "no-store", ...extra }); res.end(body); };
    if (p === "/") return (res.writeHead(200, { "content-type": "text/html" }), res.end("<!doctype html><title>fixture</title><p>ok</p>"));
    if (p === "/sw.js") return (res.writeHead(200, { "content-type": "application/javascript", "cache-control": "no-store" }), res.end(sw));
    if (p === "/json") return json(200, JSON_BODY);
    if (p === "/empty200") return json(200, "");
    if (p === "/204") return (res.writeHead(204, { "cache-control": "no-store" }), res.end());
    if (p === "/etag") { if (req.headers["if-none-match"] === ETAG) { res.writeHead(304, { etag: ETAG }); return res.end(); } return json(200, JSON_BODY, { etag: ETAG, "cache-control": "no-cache" }); }
    if (p === "/cached") return json(200, JSON_BODY, { "cache-control": "max-age=3600" });
    if (p === "/r1") return (res.writeHead(301, { location: "/r2" }), res.end());
    if (p === "/r2") return (res.writeHead(302, { location: "/json" }), res.end());
    if (p === "/slow") { res.writeHead(200, { "content-type": "application/json", "cache-control": "no-store" }); res.write('{"part":1'); setTimeout(() => { try { res.end(',"part2":2}'); } catch {} }, 1500); return; }
    if (p === "/sw-pass") return json(200, JSON_BODY);
    if (p === "/err500") return json(500, JSON.stringify({ error: "boom" }));
    if (p === "/reset") { req.socket.destroy(); return; }
    res.writeHead(404, { "content-type": "application/json" }); res.end('{"error":"nf"}');
  });
  return new Promise(r => srv.listen(port, "127.0.0.1", () => r(srv)));
}
