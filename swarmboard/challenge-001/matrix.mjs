// Acceptance matrix for x402#3446. IMPL=baseline|candidate selects the module under test.
const impl = process.env.IMPL;
const modPath = impl === "baseline"
  ? new URL("./harness/node_modules/@x402/extensions/dist/esm/bazaar/index.mjs", import.meta.url).href
  : new URL("./x402/typescript/packages/extensions/dist/esm/bazaar/index.mjs", import.meta.url).href;
const { validateDiscoveryExtension } = await import(modPath);
const codegen = (() => { try { new Function("return 1"); return "allowed"; } catch { return "DISALLOWED"; } })();
const simple = { type: "object", properties: { name: { type: "string" } }, required: ["name"] };
const fmt = { type: "object", properties: { url: { type: "string", format: "uri" }, at: { type: "string", format: "date-time" } } };
const cases = [
  ["valid info, simple schema",            { schema: simple, info: { name: "demo" } }],
  ["missing required",                     { schema: simple, info: {} }],
  ["wrong type",                           { schema: simple, info: { name: 5 } }],
  ["const mismatch",                       { schema: { type:"object", properties:{ kind:{ type:"string", const:"http" } } }, info: { kind: "mcp" } }],
  ["valid format pair (uri, date-time)",   { schema: fmt, info: { url: "https://example.com/x", at: "2026-09-13T22:00:00Z" } }],
  ["INVALID format pair (uri, date-time)", { schema: fmt, info: { url: "not a uri", at: "yesterday" } }],
  ["unknown keyword in schema",            { schema: { type:"object", foo: 1, properties:{ a:{ type:"string", bar: 2 } } }, info: { a: "x" } }],
  ["non-object schema (string)",           { schema: "nope", info: { a: 1 } }],
  ["non-object schema (true)",             { schema: true, info: { a: 1 } }],
  ["external $ref",                        { schema: { properties: { a: { $ref: "https://evil.example/s.json" } } }, info: { a: 1 } }],
  ["unresolvable fragment $ref",           { schema: { properties: { a: { $ref: "#/$defs/missing" } } }, info: { a: 1 } }],
  ["malformed regex pattern",              { schema: { properties: { a: { type:"string", pattern: "(" } } }, info: { a: "x" } }],
  ["unsupported dialect $schema (draft-04)", { schema: { $schema: "http://json-schema.org/draft-04/schema#", type:"object", properties:{ a:{ type:"string" } }, required:["a"] }, info: { } }],
];
console.log(`IMPL=${impl} node=${process.version} codegen=${codegen}`);
for (const [name, ext] of cases) {
  const origErr = console.error; console.error = () => {}; // silence Ajv's compile dump
  const r = validateDiscoveryExtension(ext);
  console.error = origErr;
  const e = r.errors ? r.errors.map(s => s.length > 70 ? s.slice(0, 67) + "..." : s).join(" | ") : "";
  console.log(`  ${name.padEnd(42)} valid=${String(r.valid).padEnd(5)} ${e}`);
}
// timing: 200 calls, same schema object, then 200 calls with a fresh schema object each time
const origErr = console.error; console.error = () => {};
let t = performance.now(); for (let i = 0; i < 200; i++) validateDiscoveryExtension({ schema: simple, info: { name: "demo" } });
const same = (performance.now() - t) / 200;
t = performance.now(); for (let i = 0; i < 200; i++) validateDiscoveryExtension({ schema: { ...simple, properties: { [`f${i}`]: { type: "string" } } }, info: { [`f${i}`]: "x" } });
const fresh = (performance.now() - t) / 200;
console.error = origErr;
console.log(`  per-call (Node, this machine): same schema object ${same.toFixed(3)} ms, fresh schema each call ${fresh.toFixed(3)} ms`);
