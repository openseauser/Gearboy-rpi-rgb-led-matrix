import { Validator } from "@cfworker/json-schema";
const schema = { type: "object", properties: { name: { type: "string" }, when: { type: "string", format: "date-time" } }, required: ["name"] };
const v = new Validator(schema, "2020-12", false);
console.log("valid:", JSON.stringify(v.validate({ name: "demo" })));
console.log("missing:", JSON.stringify(v.validate({})));
console.log("badtype:", JSON.stringify(v.validate({ name: 5 })));
console.log("badformat:", JSON.stringify(v.validate({ name: "x", when: "not-a-date" })));
console.log("unknown-keyword:", JSON.stringify(new Validator({ type:"object", foo: 1, properties:{a:{type:"string", bar: 2}} }, "2020-12", false).validate({a:"x"})));
try { console.log("non-object-schema:", JSON.stringify(new Validator("nope", "2020-12", false).validate({a:"x"}))); } catch (e) { console.log("non-object-schema threw:", String(e)); }
try { console.log("bad-ref:", JSON.stringify(new Validator({ $ref: "#/definitions/missing" }, "2020-12", false).validate({}))); } catch (e) { console.log("bad-ref threw:", String(e)); }
try { console.log("bad-pattern:", JSON.stringify(new Validator({ type:"string", pattern: "(" }, "2020-12", false).validate("x"))); } catch (e) { console.log("bad-pattern threw:", String(e)); }
