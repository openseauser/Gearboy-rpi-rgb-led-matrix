// Same harness as the baseline run, except the import points at the locally built
// candidate dist instead of the published @x402/extensions@2.25.0 package.
import { validateDiscoveryExtension } from "../../dist/esm/bazaar/index.mjs";
const ext = {
  schema: { type: "object", properties: { name: { type: "string" } }, required: ["name"] },
  info: { name: "demo" },
};
const bad = { schema: ext.schema, info: { name: 5 } };
export default {
  async fetch(req) {
    const url = new URL(req.url);
    const target = url.pathname === "/bad" ? bad : ext;
    const t0 = Date.now();
    let result, threw = null;
    try { result = validateDiscoveryExtension(target); } catch (e) { threw = String(e); }
    return Response.json({ result, threw, ms: Date.now() - t0 });
  },
};
