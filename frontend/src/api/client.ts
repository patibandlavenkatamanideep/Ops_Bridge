// Thin fetch wrapper: same-origin cookies, CSRF header on writes, typed error bodies.
import type { components } from "./schema";

export type Me = components["schemas"]["MeOut"];
export type Membership = components["schemas"]["MembershipOut"];
export type CaseOut = components["schemas"]["CaseOut"];
export type QueuePage = components["schemas"]["QueuePage"];
export type QueueItem = components["schemas"]["QueueItemOut"];
export type CaseCreate = components["schemas"]["CaseCreateIn"];
export type Investigation = components["schemas"]["InvestigationOut"];
export type Run = components["schemas"]["RunOut"];
export type DocumentList = components["schemas"]["DocumentList"];
export type DocumentItem = components["schemas"]["DocumentOut"];
export type VersionDetail = components["schemas"]["VersionDetailOut"];
export type FieldItem = components["schemas"]["FieldOut"];
export type ProposalItem = components["schemas"]["ProposalOut"];
export type ProposalList = components["schemas"]["ProposalList"];
export type OperationItem = components["schemas"]["OperationOut"];
export type OperationList = components["schemas"]["OperationList"];
export type DecisionResult = components["schemas"]["DecisionOut"];

/** A finding as stored in an investigation version (fields omitted for requesters). */
export interface Finding {
  finding_id: string;
  finding_type: string;
  status: string;
  reason_code: string;
  summary: string;
  po_line_number: string | null;
  invoice_line_numbers: string[];
  values?: Record<string, string>;
  evidence?: Citation[];
}

export interface Citation {
  document_id: string;
  version_id: string;
  sha256: string;
  locator: string;
  field: string;
  classification: string;
  status?: string;
  document_title?: string | null;
  version_no?: number | null;
  value?: string | null;
  field_status?: string | null;
  excerpt?: string | null;
}

export class ApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly correlationId: string | null;
  readonly retryable: boolean;
  readonly details: Record<string, unknown>;

  constructor(status: number, code: string, message: string, correlationId: string | null,
              retryable: boolean, details: Record<string, unknown>) {
    super(message);
    this.status = status;
    this.code = code;
    this.correlationId = correlationId;
    this.retryable = retryable;
    this.details = details;
  }
}

let csrfToken: string | null = null;
export function setCsrfToken(token: string | null): void {
  csrfToken = token;
}

type Method = "GET" | "POST" | "PUT" | "PATCH" | "DELETE";

export async function api<T>(path: string, init: { method?: Method; body?: unknown;
  headers?: Record<string, string>; signal?: AbortSignal } = {}): Promise<T> {
  const method = init.method ?? "GET";
  const headers: Record<string, string> = { Accept: "application/json", ...init.headers };
  if (init.body !== undefined) headers["Content-Type"] = "application/json";
  if (method !== "GET" && csrfToken) headers["X-CSRF-Token"] = csrfToken;
  let response: Response;
  try {
    response = await fetch(path, {
      method,
      headers,
      credentials: "same-origin",
      body: init.body === undefined ? undefined : JSON.stringify(init.body),
      signal: init.signal,
    });
  } catch (err) {
    if (err instanceof DOMException && err.name === "AbortError") throw err;
    throw new ApiError(0, "network_error", "The server could not be reached.", null, true, {});
  }
  if (response.status === 204) return undefined as T;
  const text = await response.text();
  const data: unknown = text ? safeJson(text) : null;
  if (!response.ok) {
    const e = (data as { error?: Record<string, unknown> } | null)?.error ?? {};
    throw new ApiError(
      response.status,
      typeof e.code === "string" ? e.code : "http_error",
      typeof e.message === "string" ? e.message : `Request failed (${response.status}).`,
      typeof e.correlation_id === "string" ? e.correlation_id : response.headers.get("X-Correlation-Id"),
      e.retryable === true,
      (e.details as Record<string, unknown>) ?? {},
    );
  }
  return data as T;
}

function safeJson(text: string): unknown {
  try {
    return JSON.parse(text);
  } catch {
    return null;
  }
}

export const tenantPath = (tenant: string, rest: string): string =>
  `/api/v1/tenants/${encodeURIComponent(tenant)}${rest}`;
