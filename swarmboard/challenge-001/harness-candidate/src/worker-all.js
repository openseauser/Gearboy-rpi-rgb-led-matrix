// Follow-up harness: all three validators from the locally built dist.
import { validateDiscoveryExtension } from "../../dist/esm/bazaar/index.mjs";
import { validatePaymentIdentifier, validateErc20ApprovalGasSponsoringInfo } from "../../dist/esm/index.mjs";
const info = { from: "0x" + "a".repeat(40), asset: "0x" + "b".repeat(40), spender: "0x" + "c".repeat(40), amount: "1", signedTransaction: "0xabcd", version: "1" };
export default {
  async fetch(req) {
    const p = new URL(req.url).pathname;
    let result, threw = null;
    try {
      if (p === "/bazaar") result = validateDiscoveryExtension({ schema: { type: "object", properties: { name: { type: "string" } }, required: ["name"] }, info: { name: "demo" } });
      else if (p === "/pid") result = validatePaymentIdentifier({ info: { required: false, id: "abcdefghijklmnop" }, schema: { type: "object", properties: { required: { type: "boolean" } } } });
      else if (p === "/pid-bad") result = validatePaymentIdentifier({ info: { required: false }, schema: { type: "object", properties: { required: { type: "string" } } } });
      else if (p === "/erc20") result = validateErc20ApprovalGasSponsoringInfo(info);
      else if (p === "/erc20-bad") result = validateErc20ApprovalGasSponsoringInfo({ ...info, from: "0x12" });
      else result = "unknown route";
    } catch (e) { threw = String(e); }
    return Response.json({ route: p, result, threw });
  },
};
