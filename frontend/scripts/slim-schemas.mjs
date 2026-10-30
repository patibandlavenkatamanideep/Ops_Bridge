// Writes src/api/schemas.json: the OpenAPI component schemas with only the keywords the
// response validator reads (src/api/validate.ts), so the page does not ship the whole
// OpenAPI document. Run by `npm run gen:api`; CI checks it matches the backend.
import { readFileSync, writeFileSync } from "node:fs";

const KEEP = new Set(["$ref", "type", "anyOf", "enum", "const", "properties", "required", "additionalProperties", "items"]);

function slim(node) {
  if (Array.isArray(node)) return node.map(slim);
  if (node === null || typeof node !== "object") return node;
  const out = {};
  for (const [key, value] of Object.entries(node)) {
    if (!KEEP.has(key)) continue;
    if (key === "properties") {
      out.properties = Object.fromEntries(Object.entries(value).map(([name, sub]) => [name, slim(sub)]));
    } else if (key === "enum" || key === "const" || key === "required") {
      out[key] = value;
    } else {
      out[key] = slim(value);
    }
  }
  return out;
}

const spec = JSON.parse(readFileSync(new URL("../src/api/openapi.json", import.meta.url), "utf8"));
const schemas = Object.fromEntries(Object.entries(spec.components.schemas).map(([name, s]) => [name, slim(s)]));
writeFileSync(new URL("../src/api/schemas.json", import.meta.url), JSON.stringify(schemas) + "\n");
