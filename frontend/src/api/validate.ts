// Checks a successful API response against the OpenAPI schema it is documented with.
//
// The generated types say what the server sends when this page and the server were built
// together; after a deployment they can differ (an old page, a new server). Without a check a
// page renders whatever arrives: a missing field shows as empty, which on an approval screen
// means approving what was not shown. This validates the JSON Schema subset FastAPI emits
// ($ref, type, anyOf, enum, const, properties, required, additionalProperties, items) and
// ignores annotations (titles, lengths, defaults, formats).
import schemas from "./schemas.json";
import type { components } from "./schema";

export type Schemas = components["schemas"];
export type SchemaName = keyof Schemas;

type Node = {
  $ref?: string;
  type?: string | string[];
  anyOf?: Node[];
  enum?: unknown[];
  const?: unknown;
  properties?: Record<string, Node>;
  required?: string[];
  additionalProperties?: boolean | Node;
  items?: Node;
};

const SCHEMAS = schemas as unknown as Record<string, Node>; // generated: scripts/slim-schemas.mjs
const MAX_PROBLEMS = 5;

/** Problems found (empty when `value` matches the schema). */
export function validate(name: SchemaName, value: unknown): string[] {
  const problems: string[] = [];
  check({ $ref: `#/components/schemas/${String(name)}` }, value, "$", problems);
  return problems;
}

function typeOf(value: unknown): string {
  if (value === null) return "null";
  if (Array.isArray(value)) return "array";
  if (typeof value === "number") return Number.isInteger(value) ? "integer" : "number";
  return typeof value;
}

function fits(expected: string, actual: string): boolean {
  return expected === actual || (expected === "number" && actual === "integer");
}

function check(node: Node, value: unknown, path: string, problems: string[]): void {
  if (problems.length >= MAX_PROBLEMS) return;
  if (node.$ref) {
    const target = SCHEMAS[node.$ref.replace("#/components/schemas/", "")];
    if (!target) {
      problems.push(`${path}: unknown schema ${node.$ref}`);
      return;
    }
    check(target, value, path, problems);
    return;
  }
  if (node.anyOf) {
    const fitting = node.anyOf.some((alt) => {
      const sub: string[] = [];
      check(alt, value, path, sub);
      return sub.length === 0;
    });
    if (!fitting) problems.push(`${path}: matches none of the allowed shapes`);
    return;
  }
  if ("const" in node && value !== node.const) {
    problems.push(`${path}: expected ${JSON.stringify(node.const)}`);
    return;
  }
  if (node.enum && !node.enum.includes(value)) {
    problems.push(`${path}: ${JSON.stringify(value)} is not one of ${JSON.stringify(node.enum)}`);
    return;
  }
  const actual = typeOf(value);
  if (node.type) {
    const allowed = Array.isArray(node.type) ? node.type : [node.type];
    if (!allowed.some((t) => fits(t, actual))) {
      problems.push(`${path}: expected ${allowed.join(" or ")}, got ${actual}`);
      return;
    }
  }
  if (actual === "array" && node.items) {
    (value as unknown[]).forEach((item, i) => check(node.items!, item, `${path}[${i}]`, problems));
  }
  if (actual === "object" && (node.properties || node.required || node.additionalProperties !== undefined)) {
    const obj = value as Record<string, unknown>;
    for (const key of node.required ?? []) {
      if (!(key in obj)) problems.push(`${path}.${key}: missing`);
    }
    for (const [key, sub] of Object.entries(obj)) {
      const declared = node.properties?.[key];
      if (declared) check(declared, sub, `${path}.${key}`, problems);
      else if (typeof node.additionalProperties === "object") {
        check(node.additionalProperties, sub, `${path}.${key}`, problems);
      }
      // Undeclared keys are allowed: a newer server may add fields an older page ignores.
    }
  }
}
