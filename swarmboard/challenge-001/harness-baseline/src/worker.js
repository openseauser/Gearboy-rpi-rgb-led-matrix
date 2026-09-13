import { validateDiscoveryExtension } from "@x402/extensions/bazaar";
const ext = {
  schema: { type: "object", properties: { name: { type: "string" } }, required: ["name"] },
  info: { name: "demo" },
};
export default {
  async fetch(req) {
    const t0 = Date.now();
    let result, threw = null;
    try { result = validateDiscoveryExtension(ext); } catch (e) { threw = String(e); }
    return Response.json({ result, threw, ms: Date.now() - t0 });
  },
};
