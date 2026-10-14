import { ApiError, api, setCsrfToken } from "./client";
import { json, mockFetch } from "../test/utils";

afterEach(() => vi.unstubAllGlobals());

test("parses the server error body into ApiError", async () => {
  mockFetch(() =>
    json(409, { error: { code: "duplicate_case", message: "exists", correlation_id: "cid-123", retryable: false,
                         details: { existing_case_id: "c1" } } }),
  );
  const err = await api("/api/v1/x").catch((e: unknown) => e);
  expect(err).toBeInstanceOf(ApiError);
  const e = err as ApiError;
  expect([e.status, e.code, e.correlationId, e.retryable]).toEqual([409, "duplicate_case", "cid-123", false]);
  expect(e.details).toEqual({ existing_case_id: "c1" });
});

test("sends the CSRF token only on writes", async () => {
  setCsrfToken("tok");
  const fetchMock = mockFetch(() => json(200, {}));
  await api("/a");
  await api("/b", { method: "POST", body: { x: 1 } });
  const getHeaders = fetchMock.mock.calls[0]![1]!.headers as Record<string, string>;
  const postHeaders = fetchMock.mock.calls[1]![1]!.headers as Record<string, string>;
  expect(getHeaders["X-CSRF-Token"]).toBeUndefined();
  expect(postHeaders["X-CSRF-Token"]).toBe("tok");
});

test("network failure becomes a retryable error", async () => {
  mockFetch(() => Promise.reject(new TypeError("Failed to fetch")));
  const err = (await api("/a").catch((e: unknown) => e)) as ApiError;
  expect(err.code).toBe("network_error");
  expect(err.retryable).toBe(true);
});
