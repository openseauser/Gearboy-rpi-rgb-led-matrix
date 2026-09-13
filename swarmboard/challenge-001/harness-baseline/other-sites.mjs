import { validatePaymentIdentifier } from "@x402/extensions/payment-identifier";
import { validateErc20ApprovalGasSponsoringInfo } from "@x402/extensions";
const ext = { info: { required: false, id: "abcdefghijklmnop" }, schema: { type: "object", properties: { required: { type: "boolean" } } } };
console.log("payment-identifier:", JSON.stringify(validatePaymentIdentifier(ext)));
try {
  console.log("erc20:", validateErc20ApprovalGasSponsoringInfo({ version: "1", token: "0x" + "a".repeat(40), spender: "0x" + "b".repeat(40), amount: "1", signedTransaction: "0xabcd" }));
} catch (e) { console.log("erc20 THREW:", String(e).slice(0, 120)); }
