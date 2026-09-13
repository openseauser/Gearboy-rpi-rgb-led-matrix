import Ajv from "ajv/dist/2020.js";
import { Validator } from "@cfworker/json-schema";
// Cases from JSON-Schema-Test-Suite draft2020-12/multipleOf.json ("by number", "by small number", "float division = inf", "small multiple of large integer") plus monke-node's fractional pairs.
const cases = [
  ["by 1.5: 0 valid", 1.5, 0, true], ["by 1.5: 4.5 valid", 1.5, 4.5, true], ["by 1.5: 35 invalid", 1.5, 35, false],
  ["by 0.0001: 0.0075 valid (official suite)", 0.0001, 0.0075, true], ["by 0.0001: 0.00751 invalid (official suite)", 0.0001, 0.00751, false],
  ["by 0.123456789: 1e308 invalid (float division = inf)", 0.123456789, 1e308, false],
  ["by 1e-8: 12391239123 valid (small multiple of large integer)", 1e-8, 12391239123, true],
  ["by 0.1: 0.3 (monke-node)", 0.1, 0.3, null], ["by 0.1: 0.7", 0.1, 0.7, null], ["by 0.01: 0.07", 0.01, 0.07, null], ["by 0.01: 0.075", 0.01, 0.075, false],
  ["by 5: 7 invalid", 5, 7, false], ["by 5: 10 valid", 5, 10, true],
];
console.log("case".padEnd(62), "expected", "ajv  ", "cfworker");
for (const [name, m, v, exp] of cases) {
  const a = new Ajv({ strict: false, allErrors: true }).validate({ multipleOf: m }, v);
  const c = new Validator({ multipleOf: m }, "2020-12", false).validate(v).valid;
  console.log(name.padEnd(62), String(exp).padEnd(8), String(a).padEnd(5), String(c), (exp !== null && a !== exp ? " <- ajv wrong" : "") + (exp !== null && c !== exp ? " <- cfworker wrong" : ""));
}
