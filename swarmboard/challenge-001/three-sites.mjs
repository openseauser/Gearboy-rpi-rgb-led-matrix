const impl = process.env.IMPL;
const root = impl === "baseline" ? "/tmp/claude-0/-home-user-Gearboy-rpi-rgb-led-matrix/7fcb0304-f724-5fc8-a07c-bad0df250dae/scratchpad/harness/node_modules/@x402/extensions/dist/esm/index.mjs" : "/tmp/claude-0/-home-user-Gearboy-rpi-rgb-led-matrix/7fcb0304-f724-5fc8-a07c-bad0df250dae/scratchpad/x402/typescript/packages/extensions/dist/esm/index.mjs";
const baz  = impl === "baseline" ? "/tmp/claude-0/-home-user-Gearboy-rpi-rgb-led-matrix/7fcb0304-f724-5fc8-a07c-bad0df250dae/scratchpad/harness/node_modules/@x402/extensions/dist/esm/bazaar/index.mjs" : "/tmp/claude-0/-home-user-Gearboy-rpi-rgb-led-matrix/7fcb0304-f724-5fc8-a07c-bad0df250dae/scratchpad/x402/typescript/packages/extensions/dist/esm/bazaar/index.mjs";
const { validatePaymentIdentifier, validateErc20ApprovalGasSponsoringInfo } = await import(root);
const { validateDiscoveryExtension } = await import(baz);
const codegen = (() => { try { new Function("return 1"); return "allowed"; } catch { return "DISALLOWED"; } })();
console.log(`IMPL=${impl} node=${process.version} codegen=${codegen}`);
const q = () => { const e = console.error; console.error = () => {}; return () => { console.error = e; }; };
let r = q();
console.log("  bazaar valid       :", JSON.stringify(validateDiscoveryExtension({ schema: { type: "object", properties: { name: { type: "string" } }, required: ["name"] }, info: { name: "demo" } })));
console.log("  payment-id valid   :", JSON.stringify(validatePaymentIdentifier({ info: { required: false, id: "abcdefghijklmnop" }, schema: { type: "object", properties: { required: { type: "boolean" } } } })));
console.log("  payment-id invalid :", JSON.stringify(validatePaymentIdentifier({ info: { required: false }, schema: { type: "object", properties: { required: { type: "string" } } } })));
const info = { from: "0x" + "a".repeat(40), asset: "0x" + "b".repeat(40), spender: "0x" + "c".repeat(40), amount: "1", signedTransaction: "0xabcd", version: "1" };
try { console.log("  erc20 valid        :", validateErc20ApprovalGasSponsoringInfo(info)); } catch (e) { console.log("  erc20 valid        : THREW", String(e).slice(0, 80)); }
try { console.log("  erc20 invalid      :", validateErc20ApprovalGasSponsoringInfo({ ...info, from: "0x12" })); } catch (e) { console.log("  erc20 invalid      : THREW", String(e).slice(0, 80)); }
r();
