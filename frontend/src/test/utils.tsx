import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render } from "@testing-library/react";
import type { ReactElement } from "react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import type { Me } from "../api/client";
import { SessionProvider } from "../session";

export const me: Me = {
  user_id: "u1",
  username: "furn-investigator",
  display_name: "Ivy Investigator",
  csrf_token: "csrf-1",
  session_expires_at: "2026-10-14T20:00:00Z",
  session_idle_expires_at: "2026-10-14T13:00:00Z",
  memberships: [
    {
      tenant_slug: "demo-furniture",
      tenant_name: "Demo Furniture Org (synthetic)",
      tier: "standard",
      pack_id: "furniture",
      roles: ["investigator"],
      approval_scope: [],
      permissions: ["case.create", "case.read_all", "evidence.read"],
      synthetic_demo: true,
    },
  ],
};

export function renderAt(path: string, routePath: string, element: ReactElement) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <SessionProvider me={me}>
        <MemoryRouter initialEntries={[path]}>
          <Routes>
            <Route path={routePath} element={element} />
          </Routes>
        </MemoryRouter>
      </SessionProvider>
    </QueryClientProvider>,
  );
}

export function mockFetch(handler: (url: string, init?: RequestInit) => Response | Promise<Response>) {
  const fn = vi.fn(handler);
  vi.stubGlobal("fetch", fn);
  return fn;
}

export const json = (status: number, body: unknown, headers: Record<string, string> = {}) =>
  new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json", ...headers } });
