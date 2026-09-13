import { validateDiscoveryExtension } from "@x402/extensions/bazaar";
const ext = {
  schema: { type: "object", properties: { name: { type: "string" } }, required: ["name"] },
  info: { name: "demo" },
};
for (let i = 1; i <= 3; i++) {
  const t0 = performance.now();
  const r = validateDiscoveryExtension(ext);
  console.log(`call ${i}:`, JSON.stringify(r), `${(performance.now() - t0).toFixed(1)} ms`);
}
